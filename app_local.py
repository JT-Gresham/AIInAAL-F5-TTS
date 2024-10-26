print("WARNING: You are running this unofficial E2/F5 TTS demo locally, it may not be as up-to-date as the hosted version (https://huggingface.co/spaces/mrfakename/E2-F5-TTS)")

import os
import re
import torch
import intel_extensions_for_torch as ipex
import torchaudio
import gradio as gr
import numpy as np
import tempfile
from einops import rearrange
from ema_pytorch import EMA
from vocos import Vocos
from pydub import AudioSegment
from model import CFM, UNetT, DiT, MMDiT
from cached_path import cached_path
from model.utils import (
    load_checkpoint,
    get_tokenizer,
    convert_char_to_pinyin,
    save_spectrogram,
)
from transformers import pipeline
import librosa
import re
import gc
import matplotlib.pyplot as plt
import time
# import triton
# import triton.language as tl

device = "cuda" if torch.cuda.is_available() else "mps" if torch.backends.mps.is_available() else "cpu"

gc.collect()
torch.cuda.empty_cache()

print(f"Using {device} device")

# os.environ['TORCHINDUCTOR_CXX'] = 'cl'

# Global variables to store loaded models
loaded_models = {}


# --------------------- Settings -------------------- #

target_sample_rate = 24000
n_mel_channels = 100
hop_length = 256
target_rms = 0.1
nfe_step = 32  # 16, 32
cfg_strength = 2.0
ode_method = 'euler'
sway_sampling_coef = -1.0
speed = 1.0
# fix_duration = 27  # None or float (duration in seconds)
fix_duration = None

def load_model(repo_name, exp_name, model_cls, model_cfg, ckpt_step):
    ckpt_path = str(cached_path(f"hf://SWivid/{repo_name}/{exp_name}/model_{ckpt_step}.safetensors"))
    # ckpt_path = f"ckpts/{exp_name}/model_{ckpt_step}.pt"  # .pt | .safetensors
    vocab_char_map, vocab_size = get_tokenizer("Emilia_ZH_EN", "pinyin")
    model = CFM(
        transformer=model_cls(
            **model_cfg, text_num_embeds=vocab_size, mel_dim=n_mel_channels
        ),
        mel_spec_kwargs=dict(
            target_sample_rate=target_sample_rate,
            n_mel_channels=n_mel_channels,
            hop_length=hop_length,
        ),
        odeint_kwargs=dict(
            method=ode_method,
        ),
        vocab_char_map=vocab_char_map,
    ).to(device)

    model = load_checkpoint(model, ckpt_path, device, use_ema = True)

    return model

def get_model(exp_name):
    if exp_name not in loaded_models:
        if exp_name == "F5-TTS":
            F5TTS_model_cfg = dict(dim=1024, depth=22, heads=16, ff_mult=2, text_dim=512, conv_layers=4)
            base_model = load_model("F5-TTS", "F5TTS_Base", DiT, F5TTS_model_cfg, 1200000)
        elif exp_name == "E2-TTS":
            E2TTS_model_cfg = dict(dim=1024, depth=24, heads=16, ff_mult=4)
            base_model = load_model("E2-TTS", "E2TTS_Base", UNetT, E2TTS_model_cfg, 1200000)
        else:
            raise ValueError(f"Unknown model: {exp_name}")
        
        loaded_models[exp_name] = (base_model)
    
    return loaded_models[exp_name]


def chunk_text(text, max_chars=200):
    chunks = []
    current_chunk = ""
    paragraphs = text.split('\n\n')
    
    for paragraph in paragraphs:
        sentences = re.split(r'(?<=[.!?])\s+', paragraph)
        for sentence in sentences:
            if len(current_chunk) + len(sentence) <= max_chars:
                current_chunk += sentence + " "
            else:
                if current_chunk:
                    chunks.append((current_chunk.strip(), False))  # Not a new paragraph
                current_chunk = sentence + " "
        
        if current_chunk:
            chunks.append((current_chunk.strip(), True))  # Mark as new paragraph
            current_chunk = ""
    
    if current_chunk:
        chunks.append((current_chunk.strip(), True))  # Last chunk is always marked as new paragraph
    
    return chunks




def save_spectrogram(y, sr, path):
    plt.figure(figsize=(10, 4))
    D = librosa.amplitude_to_db(np.abs(librosa.stft(y)), ref=np.max)
    librosa.display.specshow(D, sr=sr, x_axis='time', y_axis='hz')
    plt.colorbar(format='%+2.0f dB')
    plt.title('Spectrogram')
    plt.tight_layout()
    plt.savefig(path)
    plt.close()

def add_pause(duration, sample_rate):
    return np.zeros(int(duration * sample_rate))

def process_audio_with_pauses(audio_chunks, sample_rate, chunk_info, remove_silence=True):
    processed_chunks = []
    for i, (chunk, is_new_paragraph) in enumerate(zip(audio_chunks, chunk_info)):
        if remove_silence:
            # Remove silence from the chunk
            non_silent_intervals = librosa.effects.split(chunk, top_db=30)
            non_silent_wave = np.concatenate([chunk[start:end] for start, end in non_silent_intervals])
            processed_chunks.append(non_silent_wave)
        else:
            processed_chunks.append(chunk)
        
        # Add pause after each chunk except the last one
        if i < len(audio_chunks) - 1:
            if is_new_paragraph:
                processed_chunks.append(add_pause(0.5, sample_rate))  # Longer pause for paragraphs (0.5 seconds)
            else:
                processed_chunks.append(add_pause(0.2, sample_rate))  # Shorter pause for sentences (0.2 seconds)

    return np.concatenate(processed_chunks)

def infer(ref_audio_orig, ref_text, gen_text, exp_name, remove_silence, spectogram_choice):
    start_time = time.time()  # Start the timer
    print(gen_text)

    chunks = chunk_text(gen_text)
    results = []
    chunk_info = []
    
    if not chunks:
        raise gr.Error("Please enter some text to generate.")
    
    # Convert reference audio
    gr.Info("Converting reference audio...")
    with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as f:
        aseg = AudioSegment.from_file(ref_audio_orig)
        audio_duration = len(aseg)
        if audio_duration > 15000:
            gr.Warning("Audio is over 15s, clipping to only first 15s.")
            aseg = aseg[:15000]
        aseg.export(f.name, format="wav")
        ref_audio = f.name

    # Load the selected model
    base_model = get_model(exp_name)

    # Transcribe reference audio if needed
    if not ref_text.strip():
        gr.Info("No reference text provided, transcribing reference audio...")
        pipe = pipeline(
            "automatic-speech-recognition",
            model="openai/whisper-large-v3-Turbo",
            torch_dtype=torch.float16,
            device=device,
        )
        ref_text = pipe(
            ref_audio,
            chunk_length_s=30,
            batch_size=128,
            generate_kwargs={"task": "transcribe"},
            return_timestamps=False,
        )['text'].strip()
        print("\nTranscribed text: ", ref_text)
        gr.Info("\nFinished transcription")
        del pipe
        torch.cuda.empty_cache()
        gc.collect()
    else:
        gr.Info("Using custom reference text...")

    # Load and preprocess reference audio
    audio, sr = torchaudio.load(ref_audio)
    if audio.shape[0] > 1:
        audio = torch.mean(audio, dim=0, keepdim=True)
    rms = torch.sqrt(torch.mean(torch.square(audio)))
    target_rms = 0.01  # Example value, adjust as needed
    if rms < target_rms:
        audio = audio * target_rms / rms
    target_sample_rate = 24000  # Example value, adjust as needed
    if sr != target_sample_rate:
        resampler = torchaudio.transforms.Resample(sr, target_sample_rate)
        audio = resampler(audio)
    audio = audio.to(device)

    # Process each chunk
    results = []
    spectrograms = []
    
    for i, (chunk, is_new_paragraph) in enumerate(chunks):
        gr.Info(f"Processing chunk {i+1}/{len(chunks)}: {chunk[:30]}...")
        
        # Prepare the text
        text_list = [ref_text + chunk]
        final_text_list = convert_char_to_pinyin(text_list)

        # Calculate duration
        ref_audio_len = audio.shape[-1] // hop_length
        zh_pause_punc = r"。，、；：？！"
        ref_text_len = len(ref_text) + len(re.findall(zh_pause_punc, ref_text))
        gen_text_len = len(chunk) + len(re.findall(zh_pause_punc, chunk))
        duration = ref_audio_len + int(ref_audio_len / ref_text_len * gen_text_len / speed)

        # Inference
        gr.Info(f"Generating audio using {exp_name}")
        with torch.inference_mode():
            generated, _ = base_model.sample(
                cond=audio,
                text=final_text_list,
                duration=duration,
                steps=nfe_step,
                cfg_strength=cfg_strength,
                sway_sampling_coef=sway_sampling_coef,
            )

        generated = generated[:, ref_audio_len:, :]
        generated_mel_spec = rearrange(generated, '1 n d -> 1 d n')
        
        del generated
        torch.cuda.empty_cache()
        
        gr.Info("Running vocoder")
        vocos = Vocos.from_pretrained("charactr/vocos-mel-24khz")
        generated_wave = vocos.decode(generated_mel_spec.cpu())
        if rms < target_rms:
            generated_wave = generated_wave * rms / target_rms

        generated_wave = generated_wave.squeeze().cpu().numpy()
        del generated_mel_spec
        torch.cuda.empty_cache()

        results.append(generated_wave)
        chunk_info.append(is_new_paragraph)

        # Generate spectrogram if requested
        if spectogram_choice == "True":
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp_spectrogram:
                spectrogram_path = tmp_spectrogram.name
                save_spectrogram(generated_wave, target_sample_rate, spectrogram_path)
            spectrograms.append(spectrogram_path)

        gc.collect()
        torch.cuda.empty_cache()

    # Process audio with pauses
    combined_audio = process_audio_with_pauses(results, target_sample_rate, chunk_info, remove_silence)
    
    # Generate final spectrogram if requested
    final_spectrogram_path = None
    if spectogram_choice == "True":
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp_spectrogram:
            final_spectrogram_path = tmp_spectrogram.name
            save_spectrogram(combined_audio, target_sample_rate, final_spectrogram_path)


    gc.collect()
    torch.cuda.empty_cache()

    end_time = time.time()  # End the timer
    generation_time = end_time - start_time  # Calculate the total time taken

    return (target_sample_rate, combined_audio), final_spectrogram_path, ref_text, f"Audio generation took {generation_time:.2f} seconds"

with gr.Blocks() as app:
    gr.Markdown("""
# E2/F5 TTS

This is an unofficial E2/F5 TTS demo. This demo supports the following TTS models:

* [E2-TTS](https://arxiv.org/abs/2406.18009) (Embarrassingly Easy Fully Non-Autoregressive Zero-Shot TTS)
* [F5-TTS](https://arxiv.org/abs/2410.06885) (A Fairytaler that Fakes Fluent and Faithful Speech with Flow Matching)

This demo is based on the [F5-TTS](https://github.com/SWivid/F5-TTS) codebase, which is based on an [unofficial E2-TTS implementation](https://github.com/lucidrains/e2-tts-pytorch).

The checkpoints support English and Chinese.

If you're having issues, try converting your reference audio to WAV or MP3, clipping it to 15s, and shortening your prompt. If you're still running into issues, please open a [community Discussion](https://huggingface.co/spaces/mrfakename/E2-F5-TTS/discussions).

**NOTE: Reference text will be automatically transcribed with Whisper if not provided. For best results, keep your reference clips short (<15s). Ensure the audio is fully uploaded before generating.**
""")

    ref_audio_input = gr.Audio(label="Reference Audio", type="filepath")
    gen_text_input = gr.Textbox(label="Text to Generate (for longer than 200 chars the app uses chunking)", lines=4)
    model_choice = gr.Radio(choices=["F5-TTS", "E2-TTS"], label="Choose TTS Model", value="F5-TTS")
    spectogram_choice = gr.Radio(choices=["True", "False"], label="Output spectrogram? (The audio generation might be faster without it)", value="True")
    generate_btn = gr.Button("Synthesize", variant="primary")
    with gr.Accordion("Advanced Settings", open=False):
        ref_text_input = gr.Textbox(label="Reference Text", info="Leave blank to automatically transcribe the reference audio. If you enter text it will override automatic transcription.", lines=2, value='')
        remove_silence = gr.Checkbox(label="Remove Silences", info="The model tends to produce silences, especially on longer audio. We can manually remove silences if needed. Note that this is an experimental feature and may produce strange results. This will also increase generation time.", value=True)
    audio_output = gr.Audio(label="Synthesized Audio")
    spectrogram_output = gr.Image(label="Spectrogram")
    generation_time_output = gr.Textbox(label="Generation Time")  

    def clear_ref_text(audio):
        return "" # if audio else gr.Textbox.update()

    ref_audio_input.change(fn=clear_ref_text, inputs=[ref_audio_input], outputs=[ref_text_input])

    generate_btn.click(infer, inputs=[ref_audio_input, ref_text_input, gen_text_input, model_choice, remove_silence, spectogram_choice], outputs=[audio_output, spectrogram_output, ref_text_input, generation_time_output])
    gr.Markdown("Unofficial demo by [mrfakename](https://x.com/realmrfakename)")

app.queue().launch()

#!/usr/bin/env python3
#
# convert-sofa-to-jamesdsp.py -- turn this repo's irs/*.sofa HRTF datasets into JamesDSP
# (Android: me.timschneeberger.rootlessjamesdsp; desktop: JDSP4Linux) compatible true-stereo
# WAV convolver kernels under jamesdsp/Convolver/.
#
# JamesDSP has no libmysofa support, so unlike EasyEffects it cannot load .sofa HRTF datasets
# directly. Instead it needs a plain 4-channel WAV in the same true-stereo layout used everywhere
# else in this repo (L->L, L->R, R->L, R->R): two virtual "stereo speakers" at +/-30 degrees
# azimuth, each speaker's measured impulse response to each ear.
#
# Algorithm (already validated interactively -- reimplemented here verbatim, do not redesign):
#   1. Read Data.IR (M x 2 x N float64), Data.SamplingRate, SourcePosition (M x 3 spherical
#      [azimuth_deg, elevation_deg, distance_m]; SOFA convention: azimuth 0 = front, increasing
#      counterclockwise = toward the listener's LEFT).
#   2. Convert every SourcePosition row to a 3D unit vector (proper spherical -> cartesian, NOT
#      naive degree subtraction) so nearest-neighbour search is wraparound-safe: dot product /
#      angular distance on unit vectors is well-behaved across the 0/360 seam, plain degree
#      differences are not.
#   3. Nearest measurement to (azimuth=+30, elevation=0) = virtual LEFT speaker; nearest to
#      (azimuth=-30, elevation=0) = virtual RIGHT speaker. Both resolve to ~0 degrees of angular
#      error for both files.
#   4. Receiver 0 = left ear, receiver 1 = right ear for both files (ReceiverPosition has
#      receiver 0 at +y, and SOFA's y-axis points toward the listener's left; ARI's own
#      ReceiverDescription attribute confirms this in words too).
#   5. Assemble ch0=Lspeaker->Lear, ch1=Lspeaker->Rear, ch2=Rspeaker->Lear, ch3=Rspeaker->Rear.
#   6. Peak-normalize the whole 4-channel buffer with a single global scale factor to exactly
#      -1.0 dBFS -- this preserves relative channel balance (ITD/ILD), unlike per-channel
#      normalization.
#   7. Write a 4-channel 32-bit float WAV at the SOFA's native sample rate (no resampling), then
#      re-open it and re-validate channel count / sample rate / finiteness / peak level.
#
# Requires: pip install h5py numpy soundfile
#
# Run: python3 scripts/convert-sofa-to-jamesdsp.py
# Idempotent: fully deterministic (no randomness), re-running overwrites jamesdsp/Convolver/*.wav
# with the same output every time. Exits non-zero if any post-write validation assertion fails.

import sys
from pathlib import Path

import h5py
import numpy as np
import soundfile as sf

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "irs"
DEST_DIR = REPO_ROOT / "jamesdsp" / "Convolver"

TARGET_PEAK_DBFS = -1.0
PEAK_TOLERANCE_DB = 0.01

# (source .sofa filename, output .wav filename)
CONVERSIONS = [
    (
        "MIT KEMAR HRTF (Normal Pinna).sofa",
        "MIT KEMAR HRTF (True Stereo, 44100Hz).wav",
    ),
    (
        "ARI HRTF (Subject NH2, DTF).sofa",
        "ARI HRTF Subject NH2 DTF (True Stereo, 48000Hz).wav",
    ),
]


def spherical_deg_to_unit_vector(azimuth_deg, elevation_deg):
    """SOFA spherical -> cartesian unit vector(s); inputs may be scalars or arrays.

    SOFA convention: azimuth 0 = front (+x), 90 = left (+y); elevation 90 = up (+z).
    """
    az = np.radians(azimuth_deg)
    el = np.radians(elevation_deg)
    x = np.cos(el) * np.cos(az)
    y = np.cos(el) * np.sin(az)
    z = np.sin(el)
    return np.stack([x, y, z], axis=-1)


def nearest_measurement(source_positions, unit_vectors, target_azimuth_deg, target_elevation_deg):
    """Angular (dot-product) nearest neighbour; wraparound-safe unlike degree subtraction.

    Returns (index, resolved_azimuth_deg, resolved_elevation_deg, angular_error_deg).
    """
    target_vec = spherical_deg_to_unit_vector(target_azimuth_deg, target_elevation_deg)
    dots = unit_vectors @ target_vec
    idx = int(np.argmax(dots))
    resolved_az, resolved_el = source_positions[idx, 0], source_positions[idx, 1]
    angular_error_deg = float(np.degrees(np.arccos(np.clip(dots[idx], -1.0, 1.0))))
    return idx, float(resolved_az), float(resolved_el), angular_error_deg


def display_azimuth(azimuth_deg):
    """Fold a raw 0..360 SOFA azimuth into (-180, 180] for human-readable printing only."""
    folded = ((azimuth_deg + 180.0) % 360.0) - 180.0
    return folded if folded != -180.0 else 180.0


def strip_nondeterministic_wav_chunks(wav_path):
    """Delete libsndfile's auto-generated 'PEAK' chunk from a written RIFF/WAVE file.

    libsndfile embeds a wall-clock timestamp in the PEAK chunk it writes for float-subtype WAV
    files, which would otherwise make byte-identical re-runs of this script impossible. The PEAK
    chunk is purely informational (a cached per-channel peak position/value) -- every chunk
    actually needed for playback (fmt, fact, data) is left untouched, and the RIFF size is
    corrected afterward.
    """
    with open(wav_path, "rb") as f:
        raw = f.read()
    if raw[0:4] != b"RIFF" or raw[8:12] != b"WAVE":
        raise ValueError(f"{wav_path.name}: not a RIFF/WAVE file")

    chunks = []
    pos = 12
    while pos + 8 <= len(raw):
        chunk_id = raw[pos:pos + 4]
        chunk_size = int.from_bytes(raw[pos + 4:pos + 8], "little")
        pad = chunk_size % 2
        end = pos + 8 + chunk_size + pad
        chunks.append((chunk_id, raw[pos:end]))
        pos = end

    kept = [chunk_bytes for chunk_id, chunk_bytes in chunks if chunk_id != b"PEAK"]
    if len(kept) == len(chunks):
        return  # nothing to strip

    body = b"".join(kept)
    new_raw = b"RIFF" + (4 + len(body)).to_bytes(4, "little") + b"WAVE" + body
    with open(wav_path, "wb") as f:
        f.write(new_raw)


def convert_one(sofa_name, wav_name):
    sofa_path = SRC_DIR / sofa_name
    wav_path = DEST_DIR / wav_name

    with h5py.File(sofa_path, "r") as sofa:
        ir = sofa["Data.IR"][:].astype(np.float64)  # (M, 2, N)
        sample_rate = int(sofa["Data.SamplingRate"][0])
        source_positions = sofa["SourcePosition"][:]  # (M, 3) [azimuth_deg, elevation_deg, distance_m]

    if ir.ndim != 3 or ir.shape[1] != 2:
        raise ValueError(f"{sofa_name}: expected Data.IR shape (M, 2, N), got {ir.shape}")

    unit_vectors = spherical_deg_to_unit_vector(source_positions[:, 0], source_positions[:, 1])

    left_idx, left_az, left_el, left_err = nearest_measurement(source_positions, unit_vectors, 30.0, 0.0)
    right_idx, right_az, right_el, right_err = nearest_measurement(source_positions, unit_vectors, -30.0, 0.0)

    print(f"[{sofa_name}]")
    print(
        f"  virtual LEFT speaker  -> measurement #{left_idx}: "
        f"azimuth={display_azimuth(left_az):+7.2f} deg (raw {left_az:6.2f}), "
        f"elevation={left_el:+6.2f} deg, angular error {left_err:.4f} deg"
    )
    print(
        f"  virtual RIGHT speaker -> measurement #{right_idx}: "
        f"azimuth={display_azimuth(right_az):+7.2f} deg (raw {right_az:6.2f}), "
        f"elevation={right_el:+6.2f} deg, angular error {right_err:.4f} deg"
    )

    # Receiver 0 = left ear, receiver 1 = right ear (see module header comment).
    left_speaker_to_left_ear = ir[left_idx, 0, :]
    left_speaker_to_right_ear = ir[left_idx, 1, :]
    right_speaker_to_left_ear = ir[right_idx, 0, :]
    right_speaker_to_right_ear = ir[right_idx, 1, :]

    # True-stereo channel order: ch0=L->L, ch1=L->R, ch2=R->L, ch3=R->R.
    buffer = np.stack(
        [
            left_speaker_to_left_ear,
            left_speaker_to_right_ear,
            right_speaker_to_left_ear,
            right_speaker_to_right_ear,
        ],
        axis=1,
    ).astype(np.float64)  # (N, 4)

    # Single global scale factor -> preserves relative channel balance (ITD/ILD).
    global_peak = float(np.max(np.abs(buffer)))
    if global_peak <= 0.0 or not np.isfinite(global_peak):
        raise ValueError(f"{sofa_name}: degenerate IR buffer, peak={global_peak}")
    target_linear = 10.0 ** (TARGET_PEAK_DBFS / 20.0)
    buffer *= target_linear / global_peak

    sf.write(str(wav_path), buffer.astype(np.float32), sample_rate, subtype="FLOAT")
    strip_nondeterministic_wav_chunks(wav_path)

    verify_output(wav_path, expected_sample_rate=sample_rate)
    print(f"  wrote {wav_path.relative_to(REPO_ROOT)} ({buffer.shape[0]} frames @ {sample_rate} Hz)")


def verify_output(wav_path, expected_sample_rate):
    """Re-open the just-written WAV and re-validate it. Raises on any failure (non-zero exit)."""
    info = sf.info(str(wav_path))
    if info.channels != 4:
        raise AssertionError(f"{wav_path.name}: expected 4 channels, got {info.channels}")
    if info.samplerate != expected_sample_rate:
        raise AssertionError(
            f"{wav_path.name}: expected {expected_sample_rate} Hz, got {info.samplerate} Hz"
        )

    data, _ = sf.read(str(wav_path), dtype="float32", always_2d=True)
    if not np.all(np.isfinite(data)):
        raise AssertionError(f"{wav_path.name}: contains NaN/Inf samples")

    peak = float(np.max(np.abs(data)))
    if peak <= 0.0:
        raise AssertionError(f"{wav_path.name}: silent output (peak={peak})")
    peak_dbfs = 20.0 * np.log10(peak)
    if abs(peak_dbfs - TARGET_PEAK_DBFS) > PEAK_TOLERANCE_DB:
        raise AssertionError(
            f"{wav_path.name}: peak {peak_dbfs:.4f} dBFS is not within "
            f"{PEAK_TOLERANCE_DB} dB of target {TARGET_PEAK_DBFS} dBFS"
        )


def main():
    DEST_DIR.mkdir(parents=True, exist_ok=True)
    for sofa_name, wav_name in CONVERSIONS:
        convert_one(sofa_name, wav_name)
    print(f"Converted {len(CONVERSIONS)} SOFA file(s) into {DEST_DIR.relative_to(REPO_ROOT)}/")


if __name__ == "__main__":
    main()

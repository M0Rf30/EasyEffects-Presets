#!/usr/bin/env node
// Generates JamesDSP (me.timschneeberger.rootlessjamesdsp on Android; JDSP4Linux on desktop)
// Parametric EQ presets -- one EqualizerAPO-format .txt file per root-level EasyEffects preset
// that carries an `equalizer#0` plugin block -- under jamesdsp/ParametricEQ/. Read-only against
// this repo's root-level preset *.json files; writes only under jamesdsp/.
//
// Format + parsing rules verified straight from JamesDSP's importer (RootlessJamesDSP
// app/src/main/java/me/timschneeberger/rootlessjamesdsp/model/ParametricEqBandList.kt,
// `fromApoString()` / `ParametricEqFilterType.fromApoLabel()`):
//   Preamp: <preampDb> dB                                     -- Preamp:\s*([-\d.]+)\s*dB, then
//                                                                 coerceIn(-30.0, 0.0)
//   Filter <n>: ON <TYPE> Fc <freq> Hz Gain <gain> dB Q <q>   -- Filter\s+\d+:\s+ON\s+(\S+)\s+
//                                                                 Fc\s+([\d.]+)\s+Hz\s+Gain\s+
//                                                                 ([-\d.]+)\s+dB\s+Q\s+([\d.]+)
// The leading "Filter <n>" number is never captured/used by the parser (it's cosmetic), so it
// only needs to be a readable, contiguous 1-based count of the filters actually written.
//
// EasyEffects band "type" -> JamesDSP APO label (ParametricEqFilterType.fromApoLabel): this repo
// only ever produces the three below across every preset with an equalizer#0 block; all three are
// natively supported (no lossy substitute needed). An unrecognized 4th type is skipped with a
// warning rather than crashing the run.
//   Bell -> PK (peaking), Lo-shelf -> LSC (low shelf), Hi-shelf -> HSC (high shelf)
//
// Every equalizer#0 in this repo (as of writing) has split-channels == false with `left`
// byte-identical to `right`, so only `left`'s bands are exported -- JamesDSP's Parametric EQ has
// no per-channel concept to hold a distinct right-channel curve anyway. If a future preset ever
// breaks that assumption, this script warns instead of silently discarding the right channel.
//
// Run: node scripts/generate-jamesdsp-eq.js

const fs = require('fs');
const path = require('path');

const REPO_ROOT = path.resolve(__dirname, '..');
const OUT_DIR = path.join(REPO_ROOT, 'jamesdsp', 'ParametricEQ');

const PREAMP_MIN_DB = -30;
const PREAMP_MAX_DB = 0;

// EasyEffects equalizer band "type" -> EqualizerAPO/JamesDSP filter label.
const TYPE_MAP = {
  'Bell': 'PK',
  'Lo-shelf': 'LSC',
  'Hi-shelf': 'HSC',
};

// Mirrors JamesDSP's own regexes (see header) so every generated line can be re-parsed and
// self-checked before it's written to disk.
const FILTER_LINE_RE = /^Filter\s+\d+:\s+ON\s+(\S+)\s+Fc\s+([\d.]+)\s+Hz\s+Gain\s+([-\d.]+)\s+dB\s+Q\s+([\d.]+)$/;
const PREAMP_LINE_RE = /^Preamp:\s*([-\d.]+)\s*dB$/;

// Strips binary floating-point noise (e.g. 0.1 + 0.2 -> 0.30000000000000004) without rounding to
// a fixed number of decimals and without ever emitting scientific notation for the magnitudes an
// audio EQ deals in -- keeps values verbatim while still matching JamesDSP's [\d.]+/[-\d.]+ groups.
function fmtNum(n) {
  return String(Math.round(n * 1e6) / 1e6);
}

function listPresetFiles() {
  return fs.readdirSync(REPO_ROOT)
    .filter((name) => name.endsWith('.json'))
    .sort();
}

function loadEqualizer(presetFile) {
  const data = JSON.parse(fs.readFileSync(path.join(REPO_ROOT, presetFile), 'utf8'));
  return (data.output && data.output['equalizer#0']) || null;
}

// Builds the full EqualizerAPO text for one preset's equalizer#0 block, self-checking every
// generated line against the exact regexes JamesDSP's importer uses.
function buildApoText(presetName, eq) {
  const numBands = eq['num-bands'];
  const left = eq.left;

  if (eq['split-channels'] || JSON.stringify(eq.left) !== JSON.stringify(eq.right)) {
    console.warn(
      `WARN: ${presetName}: split-channels is on and/or left/right differ; exporting "left" ` +
      `only -- JamesDSP's Parametric EQ has no per-channel curve to hold the right channel too.`
    );
  }

  const rawPreamp = eq['input-gain'] + eq['output-gain'];
  const preamp = Math.min(PREAMP_MAX_DB, Math.max(PREAMP_MIN_DB, rawPreamp));
  if (preamp !== rawPreamp) {
    console.warn(
      `WARN: ${presetName}: preamp ${fmtNum(rawPreamp)} dB clamped to ${fmtNum(preamp)} dB ` +
      `(JamesDSP's importer clamps Preamp to [${PREAMP_MIN_DB}, ${PREAMP_MAX_DB}] dB).`
    );
  }

  const lines = [`Preamp: ${fmtNum(preamp)} dB`];
  if (!PREAMP_LINE_RE.test(lines[0])) {
    throw new Error(`${presetName}: generated preamp line failed self-check: ${lines[0]}`);
  }

  let filterIndex = 0;
  for (let i = 0; i < numBands; i++) {
    const band = left[`band${i}`];
    if (!band) {
      console.warn(`WARN: ${presetName}: band${i} missing from equalizer#0.left (num-bands=${numBands}); skipping.`);
      continue;
    }
    const apoType = TYPE_MAP[band.type];
    if (!apoType) {
      console.warn(`WARN: ${presetName}: band${i} has unsupported type "${band.type}"; skipping.`);
      continue;
    }
    filterIndex += 1;
    const line = `Filter ${filterIndex}: ON ${apoType} Fc ${fmtNum(band.frequency)} Hz Gain ${fmtNum(band.gain)} dB Q ${fmtNum(band.q)}`;
    if (!FILTER_LINE_RE.test(line)) {
      throw new Error(`${presetName}: generated filter line failed self-check: ${line}`);
    }
    lines.push(line);
  }

  return { text: lines.join('\n') + '\n', bandCount: filterIndex, preamp };
}

function main() {
  fs.mkdirSync(OUT_DIR, { recursive: true });

  let count = 0;
  for (const presetFile of listPresetFiles()) {
    const eq = loadEqualizer(presetFile);
    if (!eq) continue;

    const presetName = path.basename(presetFile, '.json');
    const { text, bandCount, preamp } = buildApoText(presetName, eq);

    fs.writeFileSync(path.join(OUT_DIR, `${presetName}.txt`), text);
    count += 1;
    console.log(`  ${presetName}.txt: ${bandCount} band(s), preamp ${fmtNum(preamp)} dB`);
  }

  console.log(`Wrote ${count} JamesDSP Parametric EQ file(s) to ${path.relative(REPO_ROOT, OUT_DIR)}/`);
}

main();

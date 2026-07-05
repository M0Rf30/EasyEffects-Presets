#!/usr/bin/env node
// Generates jamesdsp/Convolver/ -- the JamesDSP (Android: me.timschneeberger.rootlessjamesdsp,
// desktop: JDSP4Linux) equivalent of this repo's irs/ convolver kernels.
//
// JamesDSP's Convolver loads plain multichannel WAVE files (no libmysofa/.sofa support), and its
// true-stereo channel order (L->L, L->R, R->L, R->R) is identical to EasyEffects'. That means
// every existing irs/*.irs kernel already IS a valid JamesDSP convolver file -- no re-encoding,
// resampling, or channel-order change is needed, just a verbatim byte-for-byte copy under the
// same filename. The two irs/*.sofa HRTF datasets are handled separately by
// scripts/convert-sofa-to-jamesdsp.py, which extracts a true-stereo WAV JamesDSP can actually load
// (JamesDSP cannot read .sofa directly).
//
// Run: node scripts/generate-jamesdsp-convolver.js
// Idempotent: re-running overwrites jamesdsp/Convolver/*.irs with identical bytes every time.

const fs = require('fs');
const path = require('path');

const REPO_ROOT = path.resolve(__dirname, '..');
const SRC_DIR = path.join(REPO_ROOT, 'irs');
const DEST_DIR = path.join(REPO_ROOT, 'jamesdsp', 'Convolver');

function main() {
  fs.mkdirSync(DEST_DIR, { recursive: true });

  const irsFiles = fs
    .readdirSync(SRC_DIR)
    .filter((name) => path.extname(name).toLowerCase() === '.irs')
    .sort();

  for (const name of irsFiles) {
    fs.copyFileSync(path.join(SRC_DIR, name), path.join(DEST_DIR, name));
  }

  console.log(`Copied ${irsFiles.length} .irs kernel(s) into ${path.relative(REPO_ROOT, DEST_DIR)}/`);
}

main();

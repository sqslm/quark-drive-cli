import {mkdirSync, writeFileSync} from 'node:fs';
import {join} from 'node:path';

// An original, restrained electronic score. Deterministic so the render is reproducible.
const rate = 22050;
const duration = 33;
const samples = new Float32Array(rate * duration);
let seed = 4217;
const random = () => ((seed = (seed * 1664525 + 1013904223) >>> 0) / 4294967296) * 2 - 1;
const add = (start, length, fn) => {
  const first = Math.floor(start * rate);
  const count = Math.floor(length * rate);
  for (let i = 0; i < count && first + i < samples.length; i++) samples[first + i] += fn(i / rate, i / count);
};
const note = (freq, start, length, amp = 0.1) => add(start, length, (t, p) => {
  const envelope = Math.min(1, t * 12) * Math.pow(1 - p, 1.7);
  const shimmer = Math.sin(2 * Math.PI * freq * t) + .34 * Math.sin(2 * Math.PI * freq * 2.003 * t);
  return shimmer * envelope * amp;
});
const chords = [
  [146.83, 174.61, 220.00, 329.63],
  [116.54, 174.61, 220.00, 293.66],
  [130.81, 174.61, 220.00, 329.63],
  [130.81, 164.81, 196.00, 293.66],
  [146.83, 174.61, 220.00, 329.63],
  [110.00, 164.81, 220.00, 293.66],
];
for (let section = 0; section < 6; section++) {
  const start = section * 5.5;
  chords[section].forEach((hz, i) => {
    add(start, 5.5, (t, p) => {
      const envelope = Math.sin(Math.PI * p) ** .65;
      return Math.sin(2 * Math.PI * hz * t + Math.sin(t * 1.6) * .05) * envelope * (.018 - i * .002);
    });
  });
  add(start, .55, (t, p) => Math.sin(2 * Math.PI * (74 - 38 * p) * t) * Math.exp(-t * 11) * .14);
}
for (let t = .55; t < 32; t += .55) {
  const section = Math.min(5, Math.floor(t / 5.5));
  const scale = chords[section];
  const index = Math.floor(t / .55) % 8;
  note(scale[[0, 2, 3, 2, 1, 2, 3, 2][index]] * (index % 4 === 0 ? 2 : 4), t, .44, index % 4 === 0 ? .032 : .023);
  if (index % 2 === 1) add(t, .11, (sec, p) => random() * Math.exp(-sec * 65) * .015);
}
for (const t of [5.5, 11, 16.5, 22, 27.5]) {
  add(t - .3, .9, (sec, p) => random() * Math.sin(Math.PI * p) * .012);
  note(880, t, .45, .05);
}
const fade = .8;
let peak = 0;
for (let i = 0; i < samples.length; i++) {
  const t = i / rate;
  samples[i] *= Math.min(1, t / fade, (duration - t) / 1.3);
  peak = Math.max(peak, Math.abs(samples[i]));
}
const pcm = Buffer.alloc(samples.length * 2);
for (let i = 0; i < samples.length; i++) pcm.writeInt16LE(Math.round(samples[i] / peak * 26000), i * 2);
const header = Buffer.alloc(44);
header.write('RIFF', 0); header.writeUInt32LE(36 + pcm.length, 4); header.write('WAVEfmt ', 8);
header.writeUInt32LE(16, 16); header.writeUInt16LE(1, 20); header.writeUInt16LE(1, 22);
header.writeUInt32LE(rate, 24); header.writeUInt32LE(rate * 2, 28);
header.writeUInt16LE(2, 32); header.writeUInt16LE(16, 34);
header.write('data', 36); header.writeUInt32LE(pcm.length, 40);
const publicDir = join(import.meta.dirname, '..', 'public');
mkdirSync(publicDir, {recursive: true});
const wav = join(publicDir, 'score.wav');
writeFileSync(wav, Buffer.concat([header, pcm]));
console.log(wav);

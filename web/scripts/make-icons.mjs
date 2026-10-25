// Generates the PWA icons in public/ from one SVG. Run: node scripts/make-icons.mjs
// Uses sharp, which ships with Next.js (no extra dependency).
import sharp from "sharp";
import { writeFileSync } from "node:fs";

// A yellow speech bubble holding a phone keypad on black, with the 4 key lit white
// as if just pressed: call4me presses the keypad for you. High contrast, readable at
// 48 px. The 4 is a bold sans-serif glyph stored as a path, so it looks the same without any font installed.
const keys = [
  [148, 120], [228, 120], [308, 120],
  [228, 200], [308, 200],
  [148, 280], [228, 280], [308, 280],
]
  .map(([x, y]) => `<rect x="${x}" y="${y}" width="56" height="56" rx="16" fill="#000"/>`)
  .join("");

const svg = (pad) => `
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">
  <rect width="512" height="512" fill="#000"/>
  <g transform="translate(${pad} ${pad}) scale(${(512 - 2 * pad) / 512})">
    <path d="M96 76h320a44 44 0 0 1 44 44v216a44 44 0 0 1-44 44H240l-100 80v-80H96a44 44 0 0 1-44-44V120a44 44 0 0 1 44-44z" fill="#ffd60a"/>
    ${keys}
    <rect x="140" y="194" width="72" height="72" rx="20" fill="#fff" stroke="#000" stroke-width="8"/>
    <path d="M177.09423828125 217.9921875 165.96728515625 234.4716796875H177.09423828125ZM175.40673828125 209.6337890625H186.69189453125V234.4716796875H192.30810546875V241.828125H186.69189453125V249.0H177.09423828125V241.828125H159.63916015625V233.126953125Z" fill="#000"/>
  </g>
</svg>`;

writeFileSync("public/icon.svg", svg(0).trim());
const out = [
  ["public/icon-192.png", 192, 0],
  ["public/icon-512.png", 512, 0],
  ["public/icon-maskable-512.png", 512, 64], // safe zone for Android masks
  ["public/apple-touch-icon.png", 180, 24],
];
for (const [file, size, pad] of out) {
  await sharp(Buffer.from(svg(pad))).resize(size, size).png().toFile(file);
  console.log("wrote", file);
}

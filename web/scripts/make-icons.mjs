// Generates the PWA icons in public/ from one SVG. Run: node scripts/make-icons.mjs
// Uses sharp, which ships with Next.js (no extra dependency).
import sharp from "sharp";
import { writeFileSync } from "node:fs";

// A yellow speech bubble with "c4m" on black: high contrast, readable at 48 px.
const svg = (pad) => `
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">
  <rect width="512" height="512" fill="#000"/>
  <g transform="translate(${pad} ${pad}) scale(${(512 - 2 * pad) / 512})">
    <path d="M96 88h320a40 40 0 0 1 40 40v200a40 40 0 0 1-40 40H236l-96 80v-80H96a40 40 0 0 1-40-40V128a40 40 0 0 1 40-40z" fill="#ffd60a"/>
    <text x="256" y="268" text-anchor="middle" font-family="Helvetica, Arial, sans-serif" font-size="150" font-weight="800" fill="#000">c4m</text>
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

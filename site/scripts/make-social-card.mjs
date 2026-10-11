// Renders public/social-card.png (1200x630). Uses the official mark (src/assets/psr-ai-mark.png). No version or counts, so it never goes stale.
import sharp from 'sharp';
import { writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

const font = 'Segoe UI, Arial, sans-serif';
const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="630" viewBox="0 0 1200 630">
<rect width="1200" height="630" fill="#0e1116"/>
<g fill="none" stroke="#7aa2f7" stroke-opacity="0.18" stroke-width="2"><circle cx="1010" cy="470" r="150"/><circle cx="1010" cy="470" r="230"/></g>
<rect x="60" y="138" width="92" height="92" rx="46" fill="#ffffff"/>
<text x="176" y="198" font-family="${font}" font-size="44" font-weight="700" fill="#e8eeff">PSR.ai</text>
<text x="60" y="340" font-family="${font}" font-size="68" font-weight="700" fill="#ffffff">Record any workflow once.</text>
<text x="60" y="420" font-family="${font}" font-size="68" font-weight="700" fill="#ffffff">Let AI agents understand it.</text>
<text x="62" y="500" font-family="${font}" font-size="30" fill="#a9b7d6">Screens in, structured context out.</text>
<text x="62" y="580" font-family="${font}" font-size="24" fill="#7aa2f7">maioio.github.io/psr-ai</text>
</svg>`;
const mark = await sharp(fileURLToPath(new URL('../src/assets/psr-ai-mark.png', import.meta.url)))
  .resize({ width: 64 }).png().toBuffer();
const out = fileURLToPath(new URL('../public/social-card.png', import.meta.url));
writeFileSync(out, await sharp(Buffer.from(svg)).composite([{ input: mark, left: 74, top: 154 }]).png().toBuffer());
console.log('wrote', out);

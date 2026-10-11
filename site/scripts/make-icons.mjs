// Derives the site's brand assets from the official PSR.ai mark, the browser
// extension icons in src/psr_ai/extension/. There is no vector source, so every
// output is cut from these rasters. Run with `npm run icons`.
//
//   src/assets/psr-ai-mark.png   trimmed mark (header, Starlight logo, social card)
//   public/favicon-32.png        the official 32px icon, unchanged
//   public/favicon.ico           16px and 32px official icons, PNG-in-ICO
//   public/apple-touch-icon.png  180px, opaque, upscaled from the 128px icon
import sharp from 'sharp';
import { readFileSync, writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

const src = (size) => fileURLToPath(new URL(`../../src/psr_ai/extension/icon${size}.png`, import.meta.url));
const out = (path) => fileURLToPath(new URL(`../${path}`, import.meta.url));
const DISC = '#181a20'; // the mark's own disc colour, so the apple icon reads as one shape

// Trim the transparent margin; the corner pixel is fully transparent.
await sharp(src(128)).trim().png().toFile(out('src/assets/psr-ai-mark.png'));

writeFileSync(out('public/favicon-32.png'), readFileSync(src(32)));

// ICO container holding PNG images (supported by every current browser).
const pngs = [16, 32].map((s) => ({ size: s, data: readFileSync(src(s)) }));
const header = Buffer.alloc(6 + 16 * pngs.length);
header.writeUInt16LE(0, 0); // reserved
header.writeUInt16LE(1, 2); // type: icon
header.writeUInt16LE(pngs.length, 4);
let offset = header.length;
pngs.forEach(({ size, data }, i) => {
  const e = 6 + 16 * i;
  header.writeUInt8(size, e); // width
  header.writeUInt8(size, e + 1); // height
  header.writeUInt8(0, e + 2); // palette size
  header.writeUInt8(0, e + 3); // reserved
  header.writeUInt16LE(1, e + 4); // colour planes
  header.writeUInt16LE(32, e + 6); // bits per pixel
  header.writeUInt32LE(data.length, e + 8);
  header.writeUInt32LE(offset, e + 12);
  offset += data.length;
});
writeFileSync(out('public/favicon.ico'), Buffer.concat([header, ...pngs.map((p) => p.data)]));

// sharp applies composite after every other operation, so flatten in a second pass.
const disc = await sharp(src(128)).trim().resize(152, 152, { kernel: 'lanczos3' }).png().toBuffer();
const layered = await sharp({ create: { width: 180, height: 180, channels: 3, background: DISC } })
  .composite([{ input: disc, left: 14, top: 14 }])
  .png()
  .toBuffer();
await sharp(layered).flatten({ background: DISC }).removeAlpha().png().toFile(out('public/apple-touch-icon.png'));

console.log('wrote mark, favicon-32.png, favicon.ico, apple-touch-icon.png');

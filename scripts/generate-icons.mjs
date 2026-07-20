/**
 * Generates the Lumi orb icon assets from an analytic description of the mark.
 *
 * Pure Node: PNG and ICO are written byte-by-byte with zlib, so the build needs
 * no image toolchain, no native module, and no network. `build/icon-master.svg`
 * is the human-editable reference for the same mark; this file is what the
 * raster assets are actually produced from. Keep the two in sync by eye — the
 * constants below are shared with the SVG.
 *
 * Run: node scripts/generate-icons.mjs
 */
import { deflateSync } from 'node:zlib'
import { mkdirSync, writeFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..')
const BUILD_DIR = join(ROOT, 'build')
const TRAY_DIR = join(BUILD_DIR, 'tray')

/** Disc diameter as a fraction of the canvas, per the icon spec. */
const DISC_SCALE = 0.86
/** Gradient stops: cyan at the top-left, through violet, to magenta. */
const STOPS = [
  { t: 0.0, color: [0x79, 0xef, 0xff] },
  { t: 0.5, color: [0x8f, 0x9b, 0xff] },
  { t: 1.0, color: [0xdd, 0x75, 0xff] }
]
/** Specular spark centre, in disc-relative coordinates. */
const SPARK = { x: 0.3, y: 0.25 }
/** Rim colour that holds the disc edge against a light taskbar. */
const RIM = [0x2a, 0x2f, 0x6e]

const PNG_SIZES = [1024, 512, 256, 128, 64, 48, 32, 24, 16]
/** Windows requires the 256 px layer; electron-builder rejects anything smaller. */
const ICO_SIZES = [256, 128, 64, 48, 32, 24, 16]

function lerp(a, b, t) {
  return a + (b - a) * t
}

function sampleGradient(t) {
  const clamped = Math.min(1, Math.max(0, t))
  for (let i = 0; i < STOPS.length - 1; i += 1) {
    const from = STOPS[i]
    const to = STOPS[i + 1]
    if (clamped <= to.t) {
      const local = (clamped - from.t) / (to.t - from.t)
      return [
        lerp(from.color[0], to.color[0], local),
        lerp(from.color[1], to.color[1], local),
        lerp(from.color[2], to.color[2], local)
      ]
    }
  }
  return STOPS[STOPS.length - 1].color.slice()
}

/**
 * Renders one RGBA pixel of the mark at normalised canvas coordinates.
 *
 * `detail` follows the spec's ladder: 'full' keeps the bloom, 'solid' drops it,
 * and 'flat' reduces to a two-stop disc with a single spark dot and a heavier
 * rim so the mark survives 16-32 px.
 */
function shade(nx, ny, detail, rimBoost) {
  const radius = DISC_SCALE / 2
  const dx = nx - 0.5
  const dy = ny - 0.5
  const distance = Math.hypot(dx, dy)
  if (distance > radius) {
    return [0, 0, 0, 0]
  }

  // Gradient runs along the top-left -> bottom-right diagonal of the disc.
  const diagonal = ((dx / radius) * 0.7071 + (dy / radius) * 0.7071 + 1) / 2
  let [r, g, b] = sampleGradient(diagonal)

  if (detail !== 'flat') {
    // Inner glow lifts the centre slightly so the disc reads as a sphere.
    const glow = Math.max(0, 1 - distance / radius) ** 2 * 0.18
    r = lerp(r, 255, glow)
    g = lerp(g, 255, glow)
    b = lerp(b, 255, glow)
  }

  // Specular spark.
  const sparkDx = nx - (0.5 + (SPARK.x - 0.5) * DISC_SCALE)
  const sparkDy = ny - (0.5 + (SPARK.y - 0.5) * DISC_SCALE)
  const sparkDistance = Math.hypot(sparkDx, sparkDy)
  const sparkRadius = radius * (detail === 'flat' ? 0.26 : 0.3)
  if (sparkDistance < sparkRadius) {
    const falloff = 1 - sparkDistance / sparkRadius
    // A flat spark is a solid dot; larger sizes get a soft bloom.
    const intensity = detail === 'full' ? falloff ** 2 * 0.85 : detail === 'solid' ? falloff ** 3 * 0.9 : (falloff > 0.55 ? 0.92 : 0)
    r = lerp(r, 255, intensity)
    g = lerp(g, 255, intensity)
    b = lerp(b, 255, intensity)
  }

  // Rim: a darker ring just inside the edge so the disc holds against light chrome.
  const rimWidth = radius * rimBoost
  if (distance > radius - rimWidth) {
    const rimAmount = ((distance - (radius - rimWidth)) / rimWidth) * (detail === 'flat' ? 0.7 : 0.4)
    r = lerp(r, RIM[0], rimAmount)
    g = lerp(g, RIM[1], rimAmount)
    b = lerp(b, RIM[2], rimAmount)
  }

  return [r, g, b, 255]
}

/** Renders the mark at `size` px with 4x supersampling for clean edges. */
function render(size, options = {}) {
  const detail = size >= 128 ? 'full' : size >= 48 ? 'solid' : 'flat'
  const rimBoost = options.rimBoost ?? (size >= 128 ? 0.012 : size >= 48 ? 0.03 : 0.07)
  const samples = 4
  const pixels = Buffer.alloc(size * size * 4)

  for (let y = 0; y < size; y += 1) {
    for (let x = 0; x < size; x += 1) {
      let r = 0
      let g = 0
      let b = 0
      let a = 0
      for (let sy = 0; sy < samples; sy += 1) {
        for (let sx = 0; sx < samples; sx += 1) {
          const nx = (x + (sx + 0.5) / samples) / size
          const ny = (y + (sy + 0.5) / samples) / size
          const [pr, pg, pb, pa] = shade(nx, ny, detail, rimBoost)
          const weight = pa / 255
          r += pr * weight
          g += pg * weight
          b += pb * weight
          a += pa
        }
      }
      const total = samples * samples
      const alpha = a / total
      const coverage = alpha / 255
      const offset = (y * size + x) * 4
      // Un-premultiply so the edge keeps its colour at partial coverage.
      pixels[offset] = coverage > 0 ? Math.round(Math.min(255, r / total / coverage)) : 0
      pixels[offset + 1] = coverage > 0 ? Math.round(Math.min(255, g / total / coverage)) : 0
      pixels[offset + 2] = coverage > 0 ? Math.round(Math.min(255, b / total / coverage)) : 0
      pixels[offset + 3] = Math.round(alpha)
    }
  }

  return pixels
}

const CRC_TABLE = (() => {
  const table = new Int32Array(256)
  for (let n = 0; n < 256; n += 1) {
    let c = n
    for (let k = 0; k < 8; k += 1) {
      c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1
    }
    table[n] = c
  }
  return table
})()

function crc32(buffer) {
  let c = 0xffffffff
  for (const byte of buffer) {
    c = CRC_TABLE[(c ^ byte) & 0xff] ^ (c >>> 8)
  }
  return (c ^ 0xffffffff) >>> 0
}

function pngChunk(type, data) {
  const length = Buffer.alloc(4)
  length.writeUInt32BE(data.length, 0)
  const typeAndData = Buffer.concat([Buffer.from(type, 'ascii'), data])
  const crc = Buffer.alloc(4)
  crc.writeUInt32BE(crc32(typeAndData), 0)
  return Buffer.concat([length, typeAndData, crc])
}

function encodePng(size, pixels) {
  const header = Buffer.alloc(13)
  header.writeUInt32BE(size, 0)
  header.writeUInt32BE(size, 4)
  header[8] = 8 // bit depth
  header[9] = 6 // truecolour with alpha
  header[10] = 0
  header[11] = 0
  header[12] = 0

  // One filter byte (none) per scanline.
  const raw = Buffer.alloc(size * (size * 4 + 1))
  for (let y = 0; y < size; y += 1) {
    const rowStart = y * (size * 4 + 1)
    raw[rowStart] = 0
    pixels.copy(raw, rowStart + 1, y * size * 4, (y + 1) * size * 4)
  }

  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    pngChunk('IHDR', header),
    pngChunk('IDAT', deflateSync(raw, { level: 9 })),
    pngChunk('IEND', Buffer.alloc(0))
  ])
}

/**
 * Encodes a 32-bit BMP/DIB payload for an ICO entry. Small layers use this
 * rather than PNG because some Windows surfaces still read the DIB form.
 */
function encodeIcoBmp(size, pixels) {
  const header = Buffer.alloc(40)
  header.writeUInt32LE(40, 0)
  header.writeInt32LE(size, 4)
  header.writeInt32LE(size * 2, 8) // doubled: colour rows + mask rows
  header.writeUInt16LE(1, 12)
  header.writeUInt16LE(32, 14)
  header.writeUInt32LE(0, 16)
  header.writeUInt32LE(size * size * 4, 20)

  // BGRA, bottom-up.
  const body = Buffer.alloc(size * size * 4)
  for (let y = 0; y < size; y += 1) {
    const source = (size - 1 - y) * size * 4
    for (let x = 0; x < size; x += 1) {
      const from = source + x * 4
      const to = (y * size + x) * 4
      body[to] = pixels[from + 2]
      body[to + 1] = pixels[from + 1]
      body[to + 2] = pixels[from]
      body[to + 3] = pixels[from + 3]
    }
  }

  // The AND mask is unused for 32-bit icons but must still be present.
  const maskRowBytes = Math.ceil(size / 32) * 4
  const mask = Buffer.alloc(maskRowBytes * size)

  return Buffer.concat([header, body, mask])
}

function encodeIco(entries) {
  const directory = Buffer.alloc(6 + entries.length * 16)
  directory.writeUInt16LE(0, 0)
  directory.writeUInt16LE(1, 2)
  directory.writeUInt16LE(entries.length, 4)

  let offset = directory.length
  const payloads = []
  entries.forEach((entry, index) => {
    const at = 6 + index * 16
    directory[at] = entry.size >= 256 ? 0 : entry.size
    directory[at + 1] = entry.size >= 256 ? 0 : entry.size
    directory[at + 2] = 0
    directory[at + 3] = 0
    directory.writeUInt16LE(1, at + 4)
    directory.writeUInt16LE(32, at + 6)
    directory.writeUInt32LE(entry.data.length, at + 8)
    directory.writeUInt32LE(offset, at + 12)
    offset += entry.data.length
    payloads.push(entry.data)
  })

  return Buffer.concat([directory, ...payloads])
}

mkdirSync(BUILD_DIR, { recursive: true })
mkdirSync(TRAY_DIR, { recursive: true })

const rendered = new Map()
for (const size of PNG_SIZES) {
  const pixels = render(size)
  rendered.set(size, pixels)
  writeFileSync(join(BUILD_DIR, `icon-${size}.png`), encodePng(size, pixels))
}

const icoEntries = ICO_SIZES.map((size) => {
  const pixels = rendered.get(size) ?? render(size)
  return {
    size,
    // PNG-compressed 256 layer, DIB for the rest.
    data: size === 256 ? encodePng(size, pixels) : encodeIcoBmp(size, pixels)
  }
})
writeFileSync(join(BUILD_DIR, 'icon.ico'), encodeIco(icoEntries))

// Tray variant: the flat small mark with a heavier rim for a dense tray.
for (const size of [16, 32]) {
  writeFileSync(join(TRAY_DIR, `tray-${size}.png`), encodePng(size, render(size, { rimBoost: 0.1 })))
}

console.log(`Wrote ${PNG_SIZES.length} PNGs, icon.ico (${ICO_SIZES.length} layers), and 2 tray PNGs to build/`)

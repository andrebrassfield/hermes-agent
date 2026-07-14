import { vi } from 'vitest'

// jsdom does not implement `CSS.escape` (real browsers, and therefore the
// Electron renderer, have shipped it since ~2016). `timeline.tsx` calls it on
// message ids to build `[data-message-id="…"]` selectors, so any test that
// mounts <Thread> dies without it.
//
// Polyfilling here — rather than branching the production call site — keeps
// `timeline.tsx` byte-identical to `CSS.escape(...)`. But the polyfill only
// earns its keep if it matches real `CSS.escape` semantics: a test env that
// escapes DIFFERENTLY from production is worse than none, because selectors
// would silently stop matching for reasons unrelated to the code under test.
//
// Per CSSOM: the trailing space belongs ONLY to hex escapes (a leading digit),
// never to the backslash-the-character path. `CSS.escape('a.b')` is `a\.b`,
// not `a\. b`.
const cssEscape = (value: string): string => {
  const input = String(value)
  let out = ''

  for (let i = 0; i < input.length; i++) {
    const ch = input.charAt(i)
    const code = input.charCodeAt(i)
    const isDigit = code >= 0x30 && code <= 0x39

    // A leading digit cannot be written as `\1` — it must be a hex escape, and
    // the space is the terminator that ends the hex run.
    if (i === 0 && isDigit) {
      out += `\\${code.toString(16)} `
      continue
    }

    const isSafe =
      code >= 0x80 ||
      isDigit ||
      ch === '-' ||
      ch === '_' ||
      (code >= 0x41 && code <= 0x5a) ||
      (code >= 0x61 && code <= 0x7a)

    out += isSafe ? ch : `\\${ch}`
  }

  return out
}

// Preserve anything jsdom does provide on CSS (e.g. `supports`) — only fill the
// gap, never replace the object wholesale.
const existing = (globalThis as { CSS?: Partial<typeof CSS> }).CSS

if (!existing?.escape) {
  vi.stubGlobal('CSS', { ...(existing ?? {}), escape: cssEscape })
}

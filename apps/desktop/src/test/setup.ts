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
// This is the CSSOM `CSS.escape` algorithm implemented in full, not an
// approximation of it. Verified against the web-platform-tests vectors in
// css/cssom/escape.html. Do not "simplify" it — every branch below is a WPT
// case, and the cheap version (backslash anything non-alphanumeric) silently
// gets six of them wrong.
const cssEscape = (value: string): string => {
  const input = String(value)
  const first = input.charCodeAt(0)

  // A lone "-" is not a valid identifier and must be escaped.
  if (input.length === 1 && first === 0x2d) {
    return `\\${input}`
  }

  let out = ''

  for (let i = 0; i < input.length; i++) {
    const code = input.charCodeAt(i)

    // NULL becomes the replacement character rather than an escape.
    if (code === 0x00) {
      out += '�'
      continue
    }

    const isDigit = code >= 0x30 && code <= 0x39

    if (
      // Control characters must be hex-escaped.
      (code >= 0x01 && code <= 0x1f) ||
      code === 0x7f ||
      // A leading digit, or a digit directly after a leading "-", cannot be
      // written as `\1` — it must be a hex escape.
      (i === 0 && isDigit) ||
      (i === 1 && isDigit && first === 0x2d)
    ) {
      // The trailing space TERMINATES the hex run. It belongs here and nowhere
      // else — appending it to every escape (the bug this replaced) produces
      // `a\. b` where real CSS.escape produces `a\.b`.
      out += `\\${code.toString(16)} `
      continue
    }

    const isSafe =
      code >= 0x80 ||
      isDigit ||
      code === 0x2d ||
      code === 0x5f ||
      (code >= 0x41 && code <= 0x5a) ||
      (code >= 0x61 && code <= 0x7a)

    out += isSafe ? input.charAt(i) : `\\${input.charAt(i)}`
  }

  return out
}

// Preserve anything jsdom does provide on CSS (e.g. `supports`) — only fill the
// gap, never replace the object wholesale.
const existing = (globalThis as { CSS?: Partial<typeof CSS> }).CSS

if (!existing?.escape) {
  vi.stubGlobal('CSS', { ...(existing ?? {}), escape: cssEscape })
}

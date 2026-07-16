/**
 * Tests for buildHermesWebSocketUrl. The desktop suite already has
 * `gateway-ws-url.test.ts` covering resolveGatewayWsUrl /
 * isGatewayReauthRequired / GatewayReauthRequiredError; this file covers the
 * URL builder specifically because it has the highest concentration of
 * argument-ordering and path-joining bugs (silent in production until the
 * browser's WebSocket constructor throws).
 *
 * node:test + node:assert/strict — mirrors the desktop electron suites, no
 * second toolchain, no DOM dependency.
 */
import assert from 'node:assert/strict'
import { afterEach, beforeEach, describe, it } from 'node:test'

import { buildHermesWebSocketUrl } from './websocket-url'

interface WindowLike {
  location?: { host?: string; protocol?: string }
}

describe('buildHermesWebSocketUrl', () => {
  const ORIGINAL_WINDOW = (globalThis as { window?: WindowLike }).window

  beforeEach(() => {
    // Default: simulate a browser page on http://localhost:5174 so the
    // protocol/host fallbacks produce a real-looking URL.
    ;(globalThis as { window?: WindowLike }).window = {
      location: { host: 'localhost:5174', protocol: 'http:' }
    }
  })

  afterEach(() => {
    if (ORIGINAL_WINDOW === undefined) {
      delete (globalThis as { window?: WindowLike }).window
    } else {
      ;(globalThis as { window?: WindowLike }).window = ORIGINAL_WINDOW
    }
  })

  describe('scheme selection', () => {
    it('uses wss when protocol is https', () => {
      assert.equal(
        buildHermesWebSocketUrl({ host: 'example.com', path: '/api/ws', protocol: 'https:' }),
        'wss://example.com/api/ws'
      )
    })

    it('uses wss when protocol is already wss', () => {
      assert.equal(
        buildHermesWebSocketUrl({ host: 'example.com', path: '/api/ws', protocol: 'wss:' }),
        'wss://example.com/api/ws'
      )
    })

    it('uses ws for http', () => {
      assert.equal(
        buildHermesWebSocketUrl({ host: 'example.com', path: '/api/ws', protocol: 'http:' }),
        'ws://example.com/api/ws'
      )
    })

    it('uses ws for an unrecognized protocol (defensive default)', () => {
      assert.equal(
        buildHermesWebSocketUrl({ host: 'example.com', path: '/api/ws', protocol: 'ftp:' }),
        'ws://example.com/api/ws'
      )
    })
  })

  describe('host handling', () => {
    it('preserves a host:port pair verbatim', () => {
      assert.equal(
        buildHermesWebSocketUrl({ host: 'localhost:8080', path: '/api/ws' }),
        'ws://localhost:8080/api/ws'
      )
    })

    it('honors an explicit host when window.location.host would disagree', () => {
      // The override must win — environment-driven window.location must not
      // leak into the WebSocket URL when the caller is explicit.
      assert.equal(
        buildHermesWebSocketUrl({ host: 'internal.local', path: '/api/ws' }),
        'ws://internal.local/api/ws'
      )
    })

    it('falls back to window.location.host when host is omitted', () => {
      assert.equal(buildHermesWebSocketUrl({ path: '/api/ws' }), 'ws://localhost:5174/api/ws')
    })
  })

  describe('path joining', () => {
    it('prepends a leading slash to a path that lacks one', () => {
      assert.equal(buildHermesWebSocketUrl({ host: 'h', path: 'api/ws' }), 'ws://h/api/ws')
    })

    it('does not double a leading slash', () => {
      assert.equal(buildHermesWebSocketUrl({ host: 'h', path: '/api/ws' }), 'ws://h/api/ws')
    })

    it('strips trailing slashes from basePath and joins path', () => {
      assert.equal(
        buildHermesWebSocketUrl({ basePath: '/hermes/', host: 'h', path: '/api/ws' }),
        'ws://h/hermes/api/ws'
      )
    })

    it('accepts basePath without leading slash and adds one', () => {
      assert.equal(
        buildHermesWebSocketUrl({ basePath: 'hermes', host: 'h', path: '/api/ws' }),
        'ws://h/hermes/api/ws'
      )
    })

    it('omits the separator when basePath is empty', () => {
      assert.equal(buildHermesWebSocketUrl({ basePath: '', host: 'h', path: '/api/ws' }), 'ws://h/api/ws')
    })

    it('trims multiple trailing slashes on basePath', () => {
      assert.equal(
        buildHermesWebSocketUrl({ basePath: '/hermes///', host: 'h', path: '/api/ws' }),
        'ws://h/hermes/api/ws'
      )
    })
  })

  describe('query string', () => {
    it('omits the query separator when there are no params and no authParam', () => {
      assert.equal(buildHermesWebSocketUrl({ host: 'h', path: '/api/ws' }), 'ws://h/api/ws')
      assert.ok(!buildHermesWebSocketUrl({ host: 'h', path: '/api/ws' }).includes('?'))
    })

    it('renders extra params as query string', () => {
      assert.equal(
        buildHermesWebSocketUrl({ host: 'h', params: { foo: 'bar' }, path: '/api/ws' }),
        'ws://h/api/ws?foo=bar'
      )
    })

    it('renders authParam when no params are given', () => {
      assert.equal(
        buildHermesWebSocketUrl({
          authParam: ['token', 'abc'],
          host: 'h',
          path: '/api/ws'
        }),
        'ws://h/api/ws?token=abc'
      )
    })

    it('authParam overrides a same-named key already in params', () => {
      assert.equal(
        buildHermesWebSocketUrl({
          authParam: ['token', 'fresh'],
          host: 'h',
          params: { foo: 'bar', token: 'stale' },
          path: '/api/ws'
        }),
        'ws://h/api/ws?foo=bar&token=fresh'
      )
    })

    it('merges params before auth (auth lands last)', () => {
      // Order matters: URLSearchParams.toString() preserves insertion order,
      // and the production client reads the LAST occurrence of a repeated
      // key. Production callers depend on authParam winning against params
      // for same-named keys, AND on authParam being last (so the gateway
      // sees the most-recent ticket). This test pins both.
      assert.equal(
        buildHermesWebSocketUrl({
          authParam: ['ticket', 'fresh'],
          host: 'h',
          params: { profile: 'coder' },
          path: '/api/ws'
        }),
        'ws://h/api/ws?profile=coder&ticket=fresh'
      )
    })

    it('percent-encodes params and auth values per URLSearchParams rules', () => {
      assert.equal(
        buildHermesWebSocketUrl({
          authParam: ['token', 'a b/c?d'],
          host: 'h',
          path: '/api/ws'
        }),
        'ws://h/api/ws?token=a+b%2Fc%3Fd'
      )
    })
  })

  describe('node environment (no window)', () => {
    it('falls back to ws:// + empty host when called without a window global', () => {
      // Same shape as the desktop electron test environment: window is
      // undefined; the function must still produce a deterministic URL
      // without throwing.
      delete (globalThis as { window?: WindowLike }).window
      // Without host, fallback is empty string. Without protocol, fallback
      // is 'http:'. So: 'ws://' + '' + '/api/ws' === 'ws:///api/ws'.
      assert.equal(buildHermesWebSocketUrl({ path: '/api/ws' }), 'ws:///api/ws')
    })
  })
})

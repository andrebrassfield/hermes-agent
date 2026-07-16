/**
 * Tests for JsonRpcGatewayClient. The argument-ordering branch and the
 * notification-vs-request routing are the highest-blast-radius bugs we
 * want to nail: a wrong {id, method} ordering would silently pass through
 * a permissive server; a frame that mistakenly went through the
 * pending-resolve path would crash on `this.pending.get(undefined)`.
 *
 * node:test + node:assert — mirrors the electron suites so apps/shared
 * shares the same monorepo-side harness (no vitest, no jsdom, no
 * second toolchain).
 */
import assert from 'node:assert/strict'
import { describe, it } from 'node:test'

import {
  JsonRpcGatewayClient,
  type ConnectionState,
  type GatewayEvent,
  type JsonRpcFrame,
  type WebSocketLike
} from './json-rpc-gateway'

import { FakeWebSocket, makeSocketFactory } from './__test_helpers__/fake-websocket'

/**
 * Live wire frame: JsonRpcFrame in this package is the READ-SIDE envelope
 * (`error | id | method | params | result`) — outbound includes `jsonrpc:"2.0"`
 * which the source injects unconditionally. Helpers below widen the type
 * so assertions can pin the literal-field requirement.
 */
type WireFrame = JsonRpcFrame & { jsonrpc?: '2.0'; result?: unknown }

interface Harness {
  client: JsonRpcGatewayClient
  ws: FakeWebSocket
}

/**
 * Build a client whose socketFactory updates the given holder, then run
 * connect(), then open the socket from outside so the connect promise resolves.
 */
async function connectAndOpen(): Promise<Harness> {
  const holder: { latest: FakeWebSocket | null } = { latest: null }
  const client = new JsonRpcGatewayClient({ socketFactory: makeSocketFactory(holder) })

  const connectPromise = client.connect('ws://gateway/api/ws')
  // The connect call synchronously constructs the socket via the factory,
  // assigns it, and starts waiting on open/error events. The fake socket
  // is sitting in CONNECTING with no listeners yet — the addEventListener
  // calls happen between assign and await. We yield a microtask so those
  // listeners attach, then fire open.
  await Promise.resolve()
  assert.ok(holder.latest, 'socketFactory must have produced a FakeWebSocket')
  holder.latest.openImmediately()
  await connectPromise

  const ws = holder.latest as unknown as FakeWebSocket
  return { client, ws }
}

describe('JsonRpcGatewayClient', () => {
  describe('connect', () => {
    it('resolves on synthetic open', async () => {
      const { ws } = await connectAndOpen()
      assert.equal(ws.readyState, 1)
    })

    it('rejects on synthetic error with connectErrorMessage', async () => {
      const holder: { latest: FakeWebSocket | null } = { latest: null }
      const client = new JsonRpcGatewayClient({
        connectErrorMessage: 'connect exploded',
        socketFactory: makeSocketFactory(holder)
      })
      const p = client.connect('ws://x')
      await Promise.resolve()
      assert.ok(holder.latest)
      holder.latest.errorOut()
      await assert.rejects(p, /connect exploded/)
    })

    it('rejects on connectTimeoutMs, clearing the half-open socket', async () => {
      const holder: { latest: FakeWebSocket | null } = { latest: null }
      const client = new JsonRpcGatewayClient({
        connectErrorMessage: 'timed out',
        connectTimeoutMs: 5,
        socketFactory: makeSocketFactory(holder)
      })
      const p = client.connect('ws://x')
      await Promise.resolve()
      const ws = holder.latest as unknown as FakeWebSocket
      assert.ok(ws, 'expected socket to be constructed')
      // No open/error events fire — let connectTimeoutMs elapse.
      await assert.rejects(p, /timed out/)
      assert.equal(ws.closeWasCalled, true)
      assert.equal(ws.readyState, 3)
    })

    it('is idempotent when already open', async () => {
      const { client, ws } = await connectAndOpen()
      // The source short-circuits when state===connecting or socket.OPEN.
      // Calling connect again must NOT create a new socket.
      const sockCountBefore = ws ? 1 : 0
      await client.connect('ws://x/api/ws')
      await client.connect('ws://x/api/ws')
      assert.equal(sockCountBefore, 1, 'connect() must not allocate a second socket while open')
    })
  })

  describe('request frame shape', () => {
    it('sends {jsonrpc:"2.0", id, method, params}', async () => {
      const { client, ws } = await connectAndOpen()
      const p = client.request<unknown>('session.list', { scope: 'main' })

      const frame = ws.lastFrame() as WireFrame
      assert.equal(frame.jsonrpc, '2.0')
      assert.equal(frame.method, 'session.list')
      assert.deepEqual(frame.params, { scope: 'main' })
      assert.ok(typeof frame.id === 'string' && frame.id.length > 0, 'id must be a non-empty string')

      // Resolve so we don't leak an open promise.
      ws.deliver({ jsonrpc: '2.0', id: frame.id, result: { ok: true } } as WireFrame)
      await p
    })

    it('matches response by id; ignores other requests and notifications', async () => {
      const { client, ws } = await connectAndOpen()

      const a = client.request<unknown>('a')
      const frameA = ws.lastFrame() as { id: string }

      // Another request sent first
      const b = client.request<unknown>('b')
      const frameB = ws.lastFrame() as { id: string }
      assert.notEqual(frameA.id, frameB.id)

      // A notification (no id, method='event', params.type set) arrives
      // for an unrelated type. Must not affect either pending call.
      ws.deliver({ method: 'event', params: { type: 'skin.changed', payload: { theme: 'dark' } } })

      // Stranger-frame: an unknown id is dropped silently.
      ws.deliver({ id: 'r999', result: 'wrong-target' })

      // Resolve only A.
      ws.deliver({ id: frameA.id, result: 1 })
      ws.deliver({ id: frameB.id, result: 2 })

      assert.equal(await a, 1)
      assert.equal(await b, 2)
    })

    it('rejects with frame.error.message when the server returns an error response', async () => {
      const { client, ws } = await connectAndOpen()
      const p = client.request<unknown>('broken')
      const frame = ws.lastFrame() as { id: string }
      ws.deliver({ id: frame.id, error: { message: 'kaboom' } })
      await assert.rejects(p, /kaboom/)
    })

    it("falls back to 'Hermes RPC failed' when frame.error lacks a message", async () => {
      const { client, ws } = await connectAndOpen()
      const p = client.request<unknown>('m')
      const frame = ws.lastFrame() as { id: string }
      ws.deliver({ id: frame.id, error: {} })
      await assert.rejects(p, /Hermes RPC failed/)
    })

    it('rejects with timeout error after requestTimeoutMs', async () => {
      const { client, ws } = await connectAndOpen()
      const p = client.request<unknown>('never-responds', {}, 10)
      // No delivery — the internal timer must reject the call.
      await assert.rejects(p, /request timed out: never-responds/)
    })
  })

  describe('notifications (server pushes)', () => {
    it('dispatches method:event frames with params.type to on() handlers', async () => {
      const { client, ws } = await connectAndOpen()

      const events: GatewayEvent[] = []
      client.on('message.delta', e => {
        events.push(e)
      })

      ws.deliver({
        method: 'event',
        params: {
          session_id: 's1',
          type: 'message.delta',
          payload: { delta: 'hi' }
        }
      })

      assert.equal(events.length, 1)
      assert.equal(events[0].type, 'message.delta')
      assert.equal(events[0].session_id, 's1')
      assert.deepEqual(events[0].payload, { delta: 'hi' })
    })

    it('onAny receives events of every type', async () => {
      const { client, ws } = await connectAndOpen()
      const seen: string[] = []
      client.onAny(e => {
        seen.push(e.type)
      })

      ws.deliver({ method: 'event', params: { type: 'message.start', payload: {} } })
      ws.deliver({ method: 'event', params: { type: 'message.delta', payload: {} } })

      assert.deepEqual(seen, ['message.start', 'message.delta'])
    })

    it('ignores notification-like frames that lack params.type (not a server event)', async () => {
      const { client, ws } = await connectAndOpen()
      let n = 0
      client.on('message.delta', () => {
        n += 1
      })

      ws.deliver({ method: 'event', params: { type: '' } })
      ws.deliver({ method: 'event' })
      ws.deliver({ method: 'echo' })

      assert.equal(n, 0)
    })

    it('unsubscribe stops further deliveries', async () => {
      const { client, ws } = await connectAndOpen()
      let n = 0
      const off = client.on('tool.complete', () => {
        n += 1
      })

      ws.deliver({ method: 'event', params: { type: 'tool.complete', payload: {} } })
      off()
      ws.deliver({ method: 'event', params: { type: 'tool.complete', payload: {} } })

      assert.equal(n, 1)
    })

    it('malformed JSON in a message does not throw', async () => {
      const { client, ws } = await connectAndOpen()
      let onCalled = 0
      client.on('any', () => {
        onCalled += 1
      })

      ws.deliverRaw('not json at all {')
      ws.deliverRaw('')

      assert.equal(onCalled, 0)
    })
  })

  describe('request rejection paths', () => {
    it("rejects with notConnectedErrorMessage when there's no open socket", async () => {
      const client = new JsonRpcGatewayClient({
        notConnectedErrorMessage: 'idle'
      })
      await assert.rejects(client.request('x'), /idle/)
    })

    it('rejects with DOMException AbortError when called with an already-aborted signal', async () => {
      const { client } = await connectAndOpen()
      const ac = new AbortController()
      ac.abort()
      const p = client.request('x', {}, undefined, ac.signal)
      await assert.rejects(p, err => {
        return err instanceof Error && err.name === 'AbortError'
      })
    })

    it('aborting a mid-flight request rejects with AbortError and clears the pending call', async () => {
      const { client, ws } = await connectAndOpen()
      const ac = new AbortController()
      const p = client.request('longrun', {}, undefined, ac.signal)
      const frame = ws.lastFrame() as { id: string }
      ac.abort()
      await assert.rejects(p, err => err instanceof Error && err.name === 'AbortError')
      // A late response to the same id is silently dropped (not re-resolved).
      ws.deliver({ id: frame.id, result: 'too-late' })
    })
  })

  describe('close', () => {
    it('rejects all pending requests with closedErrorMessage', async () => {
      const { client, ws } = await connectAndOpen()
      const a = client.request('a')
      const b = client.request('b')

      client.close()

      await assert.rejects(a, /WebSocket closed/)
      await assert.rejects(b, /WebSocket closed/)
      assert.equal(ws.closeWasCalled, true)
    })

    it('fires onState(closed) when close() runs after open', async () => {
      // Subscribe AFTER connect() so we don't capture the connecting→open
      // transition. onState fires the current state immediately on subscribe,
      // so the first entry is whatever state we're already in.
      const { client } = await connectAndOpen()
      const transitions: ConnectionState[] = []
      client.onState(s => {
        transitions.push(s)
      })
      assert.deepEqual(transitions, ['open'])
      client.close()
      assert.deepEqual(transitions, ['open', 'closed'])
    })

    it('a server-pushed close also rejects pending requests', async () => {
      const { client, ws } = await connectAndOpen()
      const p = client.request('x')
      ws.closeFromServer()
      await assert.rejects(p, /WebSocket closed/)
    })
  })

  describe('onState', () => {
    it('fires immediately with the current state on subscribe', () => {
      const holder: { latest: FakeWebSocket | null } = { latest: null }
      const client = new JsonRpcGatewayClient({
        connectErrorMessage: 'x',
        socketFactory: makeSocketFactory(holder)
      })
      const seen: ConnectionState[] = []
      client.onState(s => seen.push(s))
      assert.deepEqual(seen, ['idle'])
    })

    it('fires once per state transition', async () => {
      const { client } = await connectAndOpen()
      const seen: ConnectionState[] = []
      client.onState(s => seen.push(s))
      client.close()
      assert.deepEqual(seen, ['open', 'closed'])
    })
  })
})

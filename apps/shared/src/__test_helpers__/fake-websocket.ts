/**
 * Minimal in-process WebSocketLike for JsonRpcGatewayClient tests.
 *
 * Real contract requirements (from json-rpc-gateway.ts):
 *  - addEventListener('open' | 'close' | 'error' | 'message', listener, opts?)
 *  - removeEventListener(...) — same event names
 *  - send(textOrBlob): void (sync, may throw)
 *  - close(): void
 *  - readyState: 0 CONNECTING | 1 OPEN | 2 CLOSING | 3 CLOSED
 *
 * Each fake exposes enough control to drive the client through:
 *   openImmediately(): synthetic 'open' to resolve connect().
 *   deliver(frame): dispatches a 'message' event with JSON-encoded payload.
 *   deliverRaw(text): same, with a raw string (malformed JSON scenario).
 *   errorOut(): fires 'error' (connect-time rejection).
 *   closeFromServer(): fires 'close' (server-initiated transition).
 *
 * Kept in `__test_helpers__` — not part of the public package surface.
 */
import type { JsonRpcFrame, WebSocketLike } from '../json-rpc-gateway'

export const WS_OPEN = 1
export const WS_CLOSED = 3

type EventName = 'open' | 'close' | 'error' | 'message'
// The browser's actual WebSocket sends MessageEvent | Event | ErrorEvent.
// The client treats the event payload opaquely; widen here so we can fire
// any of those shapes without gratuitous casts.
type Listener = (event: unknown) => void

interface ListenerHandle {
  listener: Listener
  once: boolean
}

/**
 * Server-originated frame (with jsonrpc discriminator) and client-originated
 * frame in the test helper. JsonRpcFrame in this package is the READ-SIDE
 * envelope (error | id | method | params | result); the wire also carries
 * `jsonrpc:"2.0"` as a discriminator for inbound frames.
 */
export interface InboundWireFrame extends JsonRpcFrame {
  jsonrpc?: '2.0'
}

export class FakeWebSocket {
  readyState = 0
  public readonly sent: string[] = []
  public closeWasCalled = false

  private readonly listeners = new Map<EventName, Set<ListenerHandle>>()

  constructor(public readonly url: string) {}

  send(payload: string): void {
    this.sent.push(payload)
  }

  close(): void {
    this.closeWasCalled = true
    this.readyState = WS_CLOSED
  }

  addEventListener(event: EventName, listener: Listener, opts?: { once?: boolean }): void {
    let bucket = this.listeners.get(event)
    if (!bucket) {
      bucket = new Set()
      this.listeners.set(event, bucket)
    }
    bucket.add({ listener, once: Boolean(opts?.once) })
  }

  removeEventListener(event: EventName, listener: Listener): void {
    const bucket = this.listeners.get(event)
    if (!bucket) return

    for (const handle of bucket) {
      if (handle.listener === listener) {
        bucket.delete(handle)
        return
      }
    }
  }

  /** ─── test-side helpers ─── not on the WebSocketLike contract. */
  openImmediately(): void {
    this.readyState = WS_OPEN
    this.fire('open', {})
  }

  deliver(frame: InboundWireFrame): void {
    this.readyState = WS_OPEN
    this.fire('message', { data: JSON.stringify(frame) })
  }

  deliverRaw(payload: string): void {
    this.readyState = WS_OPEN
    this.fire('message', { data: payload })
  }

  errorOut(): void {
    this.fire('error', new Error('boom'))
  }

  closeFromServer(): void {
    this.readyState = WS_CLOSED
    this.fire('close', {})
  }

  /** Returns the parsed outbound frame (which carries `jsonrpc:"2.0"`). */
  lastFrame(): InboundWireFrame | null {
    const last = this.sent[this.sent.length - 1]
    return last ? (JSON.parse(last) as InboundWireFrame) : null
  }

  private fire(event: EventName, payload: unknown): void {
    const bucket = this.listeners.get(event)
    if (!bucket) return

    // Iterate over a snapshot of handles so once-only handlers can remove
    // themselves without affecting iteration order.
    const snapshot = Array.from(bucket)
    for (const handle of snapshot) {
      try {
        handle.listener(payload)
      } finally {
        if (handle.once) {
          bucket.delete(handle)
        }
      }
    }
  }
}

/**
 * socketFactory-compatible function for JsonRpcGatewayClient. Returns a
 * fresh FakeWebSocket per call, typed via WebSocketLike so it slots
 * straight into GatewayClientOptions.socketFactory. The Holder's `latest`
 * is updated synchronously so tests can grab the most recent fake.
 */
export function makeSocketFactory(holder: {
  latest: FakeWebSocket | null
}): (url: string) => WebSocketLike {
  return (url: string) => {
    const ws = new FakeWebSocket(url)
    holder.latest = ws
    return ws as unknown as WebSocketLike
  }
}

import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { I18nProvider } from '@/i18n/context'
import type { ComposerAttachment } from '@/store/composer'

import { AttachmentList } from './attachments'

function makeAttachment(id: string, label = 'test.pdf'): ComposerAttachment {
  return { id, kind: 'file', label }
}

function renderWithI18n(ui: React.ReactNode) {
  return render(
    <I18nProvider configClient={{ getConfig: async () => ({}), saveConfig: async () => ({ ok: true }) }}>
      {ui}
    </I18nProvider>
  )
}

describe('AttachmentList', () => {
  afterEach(() => {
    cleanup()
  })

  it('renders valid attachments', () => {
    const attachments = [makeAttachment('a', 'doc.pdf'), makeAttachment('b', 'img.png')]
    renderWithI18n(<AttachmentList attachments={attachments} />)
    expect(screen.getByText('doc.pdf')).toBeDefined()
    expect(screen.getByText('img.png')).toBeDefined()
  })

  it('renders empty list without error', () => {
    renderWithI18n(<AttachmentList attachments={[]} />)

    // AttachmentList renders its container unconditionally; an empty list means
    // the container is present with no pills inside. `querySelector` returns
    // null when absent, so assert non-null — `toBeDefined()` would pass on null.
    const container = document.querySelector('[data-slot="composer-attachments"]')

    expect(container).not.toBeNull()
    expect(container?.children).toHaveLength(0)
  })

  it('does not crash when attachments array contains undefined entries', () => {
    // Repro: session switch can leave stale/undefined entries in the
    // attachments array, causing a TypeError at attachment.refText.
    const attachments = [
      makeAttachment('a', 'good.pdf'),
      undefined as unknown as ComposerAttachment,
      makeAttachment('b', 'also-good.png')
    ]

    expect(() => {
      renderWithI18n(<AttachmentList attachments={attachments} />)
    }).not.toThrow()

    // Only valid attachments should render
    expect(screen.getByText('good.pdf')).toBeDefined()
    expect(screen.getByText('also-good.png')).toBeDefined()
  })

  it('does not crash when attachments array contains null entries', () => {
    const attachments = [null as unknown as ComposerAttachment, makeAttachment('a', 'valid.txt')]

    expect(() => {
      renderWithI18n(<AttachmentList attachments={attachments} />)
    }).not.toThrow()

    expect(screen.getByText('valid.txt')).toBeDefined()
  })
})

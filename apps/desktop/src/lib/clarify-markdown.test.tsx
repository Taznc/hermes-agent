import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { ClarifyMarkdown, renderClarifyInline } from './clarify-markdown'

describe('renderClarifyInline', () => {
  it('renders **bold** as a real <strong>, not literal asterisks', () => {
    const { container } = render(<span>{renderClarifyInline('go with **ready** now')}</span>)

    expect(container.querySelector('strong')?.textContent).toBe('ready')
    expect(container.textContent).toBe('go with ready now')
  })

  it('renders `code` spans as real <code>, not literal backticks', () => {
    const { container } = render(<span>{renderClarifyInline('use `roadmap` stage')}</span>)

    expect(container.querySelector('code')?.textContent).toBe('roadmap')
    expect(container.textContent).toBe('use roadmap stage')
  })

  it('leaves plain text untouched', () => {
    const { container } = render(<span>{renderClarifyInline('just plain text')}</span>)

    expect(container.textContent).toBe('just plain text')
    expect(container.querySelector('strong')).toBeNull()
    expect(container.querySelector('code')).toBeNull()
  })
})

describe('ClarifyMarkdown', () => {
  it('renders bold and code spans instead of literal markdown syntax', () => {
    render(<ClarifyMarkdown text="**What is being asked:** pick `ready` or `triage`." />)

    expect(screen.getByText('What is being asked:').tagName).toBe('STRONG')
    expect(screen.getByText('ready').tagName).toBe('CODE')
    expect(screen.getByText('triage').tagName).toBe('CODE')
    // No leftover literal markdown punctuation anywhere in the rendered text.
    expect(globalThis.document.body.textContent).not.toContain('**')
    expect(globalThis.document.body.textContent).not.toContain('`')
  })

  it('splits blank-line-separated paragraphs into distinct <p> elements', () => {
    const { container } = render(<ClarifyMarkdown text={'First paragraph.\n\nSecond paragraph.'} />)

    const paragraphs = container.querySelectorAll('p')

    expect(paragraphs).toHaveLength(2)
    expect(paragraphs[0].textContent).toBe('First paragraph.')
    expect(paragraphs[1].textContent).toBe('Second paragraph.')
  })

  it('breaks a run-on "Option 1 ... Option 2 ..." sentence onto separate lines', () => {
    const { container } = render(
      <ClarifyMarkdown text="Option 1 — ready: dispatch now. Option 2 — triage: decompose first." />
    )

    const paragraphs = container.querySelectorAll('p')

    expect(paragraphs.length).toBeGreaterThanOrEqual(2)
    expect(paragraphs[0].textContent).toContain('Option 1')
    expect(paragraphs[0].textContent).not.toContain('Option 2')
    expect(paragraphs[1].textContent).toContain('Option 2')
  })

  it('renders a bulleted list as real <li> items', () => {
    const { container } = render(<ClarifyMarkdown text={'- first item\n- second item'} />)

    const items = container.querySelectorAll('ul li')

    expect(items).toHaveLength(2)
    expect(items[0].textContent).toBe('first item')
    expect(items[1].textContent).toBe('second item')
  })

  it('renders an ordered list as real <li> items', () => {
    const { container } = render(<ClarifyMarkdown text={'1. first\n2. second'} />)

    const items = container.querySelectorAll('ol li')

    expect(items).toHaveLength(2)
    expect(items[0].textContent).toBe('first')
    expect(items[1].textContent).toBe('second')
  })

  it('returns null for empty text without throwing', () => {
    const { container } = render(<ClarifyMarkdown text="" />)

    expect(container.textContent).toBe('')
  })

  it('preserves line breaks within one free-text paragraph as <br>, not a run-on join', () => {
    const { container } = render(<ClarifyMarkdown text={'line one\nline two'} />)
    const paragraphs = container.querySelectorAll('p')

    expect(paragraphs).toHaveLength(1)
    expect(paragraphs[0].querySelectorAll('br')).toHaveLength(1)
    expect(paragraphs[0].textContent).toBe('line oneline two')
  })
})

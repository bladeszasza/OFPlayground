// canvas/frontend/src/components/SlugCombobox.tsx
import { useId, useRef, useState } from 'react'
import type { SlugEntry } from '../types'

interface SlugComboboxProps {
  value: string
  onChange: (value: string) => void
  slugs: SlugEntry[]
  placeholder?: string
  style?: React.CSSProperties
}

export function SlugCombobox({ value, onChange, slugs, placeholder, style }: SlugComboboxProps) {
  const listId = useId()
  const [activeIndex, setActiveIndex] = useState(-1)
  const [open, setOpen] = useState(false)
  const closeTimer = useRef<ReturnType<typeof setTimeout> | null>(null)

  const isSlugMode = value.startsWith('@')
  const query = isSlugMode ? value.slice(1).toLowerCase() : ''

  const filtered = isSlugMode && query.length > 0
    ? slugs
        .filter(s =>
          s.slug.toLowerCase().includes(query) ||
          s.display_name.toLowerCase().includes(query)
        )
        .slice(0, 5)
    : []

  const showList = open && filtered.length > 0

  const accept = (slug: string) => {
    onChange(`@${slug}`)
    setOpen(false)
    setActiveIndex(-1)
  }

  const handleKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (!showList) return
    if (e.key === 'ArrowDown') {
      e.preventDefault()
      setActiveIndex(i => Math.min(i + 1, filtered.length - 1))
    } else if (e.key === 'ArrowUp') {
      e.preventDefault()
      setActiveIndex(i => Math.max(i - 1, 0))
    } else if ((e.key === 'Enter' || e.key === 'Tab') && activeIndex >= 0) {
      e.preventDefault()
      accept(filtered[activeIndex].slug)
    } else if (e.key === 'Escape') {
      setOpen(false)
    }
  }

  return (
    <div style={{ position: 'relative' }}>
      <input
        type="text"
        role="combobox" aria-label="System prompt or persona" aria-expanded={showList}
        aria-controls={listId} aria-autocomplete="list"
        aria-activedescendant={showList && activeIndex >= 0 ? `${listId}-${activeIndex}` : undefined}
        value={value}
        onChange={e => { onChange(e.target.value); setOpen(true); setActiveIndex(-1) }}
        onFocus={() => setOpen(true)}
        onBlur={() => { closeTimer.current = setTimeout(() => setOpen(false), 150) }}
        onKeyDown={handleKeyDown}
        placeholder={placeholder ?? '@slug or system prompt'}
        style={style}
      />
      {showList && (
        <div id={listId} role="listbox" style={{
          position: 'absolute', top: 'calc(100% + 3px)', left: 0, right: 0,
          background: '#1a2332', border: '1px solid #2a4060', borderRadius: 6,
          zIndex: 100, overflow: 'hidden', boxShadow: '0 4px 16px #00000077',
        }}>
          {filtered.map((entry, i) => (
            <div
              key={entry.slug}
              id={`${listId}-${i}`} role="option" aria-selected={i === activeIndex}
              onMouseDown={() => {
                if (closeTimer.current) clearTimeout(closeTimer.current)
                accept(entry.slug)
              }}
              style={{
                padding: '5px 10px', fontSize: 11, cursor: 'pointer',
                background: i === activeIndex ? '#1a3a5a' : 'transparent',
                color: i === activeIndex ? '#e6edf5' : '#93a4b8',
                display: 'flex', alignItems: 'center', gap: 8,
              }}
            >
              <span>@{entry.slug}</span>
              <span style={{ fontSize: 9, color: '#4a5568', marginLeft: 'auto' }}>{entry.category}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

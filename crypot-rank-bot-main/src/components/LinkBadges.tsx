import type { ResourceLink } from '@shared/cryptorank'

interface LinkBadgesProps {
  links: ResourceLink[]
}

export function LinkBadges({ links }: LinkBadgesProps) {
  if (!links.length) {
    return <span className="text-zinc-500">暂无</span>
  }

  return (
    <>
      {links.map((link) => (
        <a
          key={`${link.label}-${link.url}`}
          href={link.url}
          target="_blank"
          rel="noreferrer"
          className="inline-flex items-center rounded-full border border-cyan-400/20 bg-cyan-400/10 px-2.5 py-1 text-xs text-cyan-200 transition hover:border-cyan-300/40 hover:bg-cyan-300/15 hover:text-white"
        >
          {link.label}
        </a>
      ))}
    </>
  )
}

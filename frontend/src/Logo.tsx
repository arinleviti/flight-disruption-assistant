// The fictional Aurora Airways logo: an aircraft tail fin painted with the northern lights,
// with livery stripes and one star in the night sky. Same drawing as public/favicon.svg.
export default function Logo({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 64 64" aria-hidden="true" focusable="false">
      <defs>
        <linearGradient id="aurora-logo-gradient" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="#a08cff" />
          <stop offset="0.45" stopColor="#3fc1d4" />
          <stop offset="1" stopColor="#5fe0a6" />
        </linearGradient>
      </defs>
      <rect width="64" height="64" rx="15" fill="#0e2238" />
      <path
        d="M11 54 C 20 36, 28 22, 37 10 Q 39 7.5 42 7.5 L 50 7.5 Q 53.5 7.5 53 11 L 50.5 51 Q 50.3 54 47.3 54 Z"
        fill="url(#aurora-logo-gradient)"
      />
      <path d="M25.5 54 L 44.5 18" stroke="#0e2238" strokeWidth="2.6" strokeLinecap="round" />
      <path d="M36.5 54 L 47.5 32" stroke="#0e2238" strokeWidth="2.6" strokeLinecap="round" />
      <circle cx="17" cy="15" r="2.2" fill="#ffffff" />
    </svg>
  )
}
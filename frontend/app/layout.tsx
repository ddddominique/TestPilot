import type { Metadata } from "next";
import Link from "next/link";
import "./globals.css";

export const metadata: Metadata = { title: "TestPilot · PR testing", description: "Evidence-backed regression testing for GitHub pull requests." };
export default function Layout({ children }: { children: React.ReactNode }) {
  return <html lang="en"><body><a className="skip-link" href="#main">Skip to content</a>
    <header className="topbar"><Link className="brand" href="/"><span className="brand-icon" aria-hidden="true">⌁</span> TestPilot <span className="version">BETA</span></Link>
      <nav aria-label="Main navigation"><Link href="/">Overview</Link><a href="https://github.com/ddddominique/TestPilot" target="_blank" rel="noreferrer">GitHub ↗</a></nav>
    </header><main id="main" className="shell">{children}</main>
    <footer className="shell footer">TestPilot <span>Generated tests. Real execution. Verifiable evidence.</span></footer>
  </body></html>;
}

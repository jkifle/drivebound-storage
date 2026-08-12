import Link from "./safe-link";

export default function LandingPage() {
  return <main className="landing-page">
    <header className="landing-nav">
      <Link className="brand" href="/"><span className="brand-mark">D</span><span>Drivebound</span></Link>
      <nav aria-label="Primary navigation"><a href="#how">How it works</a><a href="#security">Security</a><Link className="secondary-button" href="/auth">Sign in</Link><Link className="primary-button" href="/auth?mode=register">Create your cloud</Link></nav>
    </header>
    <section className="landing-hero">
      <div data-reveal="left"><span className="eyebrow">Storage without the ceiling</span><h1>Your photos online. Your drives underneath.</h1><p>Drivebound turns storage you own into a private photo cloud you can reach anywhere—without locking your memories behind another monthly capacity tier.</p><div className="hero-actions"><Link className="primary-button" href="/auth?mode=register">Start with your first drive</Link><a className="secondary-button" href="#how">See how it works</a></div><div className="trust-row"><span>✓ Private accounts</span><span>✓ Verified originals</span><span>✓ Expandable storage</span></div></div>
      <div className="landing-visual" data-reveal="right"><img src="/og-neumorphic.png" alt="Drivebound library connected to expandable drives" /></div>
    </section>
    <section className="landing-section" id="how" data-reveal><span className="eyebrow">Three calm steps</span><h2>From empty drive to private cloud.</h2><div className="feature-grid"><article data-reveal data-reveal-delay="1"><b>01</b><h3>Create your account</h3><p>Verify your email and protect access with a strong password and optional two-factor authentication.</p></article><article data-reveal data-reveal-delay="2"><b>02</b><h3>Connect storage</h3><p>Index folders you already own without moving, renaming, or modifying the original files.</p></article><article data-reveal data-reveal-delay="3"><b>03</b><h3>Open it anywhere</h3><p>Upload, browse, protect, and privately share your library through one responsive experience.</p></article></div></section>
    <section className="landing-security" id="security" data-reveal><div className="step-icon success">✓</div><div><span className="eyebrow">Built around ownership</span><h2>Your account is the boundary.</h2><p>Every asset, album, device, upload, and drive lookup is filtered by the signed-in account. Sessions are revocable, security events are visible, and recovery stays under the host administrator&apos;s control.</p></div><Link className="secondary-button" href="/auth">Sign in securely</Link></section>
    <footer data-reveal><div className="brand"><span className="brand-mark">D</span>Drivebound</div><p>Your storage. Your cloud.</p></footer>
  </main>;
}

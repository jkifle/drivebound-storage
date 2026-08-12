import type { Metadata, Viewport } from "next";
import "./globals.css";
import { ScrollMotion } from "./scroll-motion";

export const metadata: Metadata = {
  metadataBase: new URL(process.env.NEXT_PUBLIC_APP_URL ?? "http://localhost:3000"),
  title: "Drivebound — Your storage. Your cloud.",
  description: "Expandable private photo storage backed by drives you own.",
  icons: { icon: "/favicon.svg", shortcut: "/favicon.svg" },
  manifest: "/manifest.webmanifest",
  openGraph: {
    title: "Drivebound",
    description: "Your storage. Your cloud.",
    images: [{ url: "/og-neumorphic.png", width: 1536, height: 1024, alt: "Drivebound — Your storage. Your cloud." }],
  },
  twitter: { card: "summary_large_image", title: "Drivebound", description: "Your storage. Your cloud.", images: ["/og-neumorphic.png"] },
};

export const viewport: Viewport = { themeColor: "#E0E5EC", colorScheme: "light" };

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="en"><body><ScrollMotion />{children}</body></html>;
}

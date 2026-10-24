import type { Metadata, Viewport } from "next";
import { AccessCodeHost } from "@/components/AccessCode";
import "./globals.css";

export const metadata: Metadata = {
  title: "call4me",
  description:
    "call4me makes customer service phone calls for Deaf and hard-of-hearing people. You read the call and type.",
  manifest: "/manifest.webmanifest",
  applicationName: "call4me",
  appleWebApp: {
    capable: true,
    title: "call4me",
    statusBarStyle: "black-translucent",
  },
  icons: {
    icon: [
      { url: "/icon-192.png", sizes: "192x192", type: "image/png" },
      { url: "/icon-512.png", sizes: "512x512", type: "image/png" },
    ],
    apple: [{ url: "/apple-touch-icon.png", sizes: "180x180" }],
  },
  formatDetection: { telephone: false },
  // Older iOS (before 16.4) only opens home-screen apps standalone with this tag.
  other: { "apple-mobile-web-app-capable": "yes" },
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  viewportFit: "cover",
  themeColor: "#000000",
  colorScheme: "dark",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="min-h-dvh antialiased">
        {children}
        <AccessCodeHost />
      </body>
    </html>
  );
}

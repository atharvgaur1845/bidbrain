import type { Metadata, Viewport } from "next";
import { Inter, JetBrains_Mono } from "next/font/google";
import Shell from "@/components/Shell";
import "./globals.css";

const inter = Inter({ variable: "--font-inter", subsets: ["latin"] });
const jbm = JetBrains_Mono({ variable: "--font-jbm", subsets: ["latin"] });

export const metadata: Metadata = {
  title: "BidBrain — bid control room",
  description: "A FalkorDB company brain for a construction bid desk: agents qualify tenders, prove absences, catch conflicting documents and show the path to every answer.",
};

export const viewport: Viewport = { themeColor: "#f7f7f5", colorScheme: "light" };

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en" className={`${inter.variable} ${jbm.variable}`}>
      <body>
        <Shell>{children}</Shell>
      </body>
    </html>
  );
}

import type { Metadata } from "next";
import { Inter, JetBrains_Mono } from "next/font/google";
import Shell from "@/components/Shell";
import "./globals.css";

const inter = Inter({ variable: "--font-inter", subsets: ["latin"] });
const jbm = JetBrains_Mono({ variable: "--font-jbm", subsets: ["latin"] });

export const metadata: Metadata = {
  title: "BidBrain — the bid desk's company brain",
  description: "A FalkorDB company brain: agents qualify tenders, prove absences, catch conflicting documents and show the path to every answer.",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en" data-theme="light" className={`${inter.variable} ${jbm.variable}`}>
      <body>
        <Shell>{children}</Shell>
      </body>
    </html>
  );
}

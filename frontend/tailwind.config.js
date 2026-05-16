/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  darkMode: "class",
  theme: {
    extend: {
      fontFamily: {
        mono: ["ui-monospace", "SFMono-Regular", "JetBrains Mono", "Menlo", "monospace"],
        sans: ["Inter", "ui-sans-serif", "system-ui", "sans-serif"],
      },
      colors: {
        // Finance-grade dark palette.
        bg: { DEFAULT: "#0e1014", elev: "#161922", elev2: "#1f2531" },
        border: { DEFAULT: "#232838", subtle: "#1a1f2a" },
        fg: { DEFAULT: "#e7ebf0", dim: "#9aa3b2", muted: "#6b7385" },
        accent: { DEFAULT: "#5fb3ff", hot: "#3b82f6" },
        good: "#4ade80",
        bad: "#f87171",
        warn: "#fbbf24",
        // Light theme overrides handled via CSS vars in styles/globals.css.
      },
      boxShadow: {
        soft: "0 1px 0 0 rgb(255 255 255 / 4%), 0 2px 12px rgb(0 0 0 / 35%)",
        glow: "0 0 0 1px rgb(95 179 255 / 35%)",
      },
      keyframes: {
        pulse_dot: {
          "0%, 100%": { opacity: "0.4" },
          "50%": { opacity: "1" },
        },
        slide_in: {
          from: { transform: "translateX(100%)", opacity: "0" },
          to: { transform: "translateX(0)", opacity: "1" },
        },
      },
      animation: {
        "pulse-dot": "pulse_dot 1.6s ease-in-out infinite",
        "slide-in": "slide_in 240ms cubic-bezier(0.2, 0.8, 0.2, 1)",
      },
    },
  },
  plugins: [],
};

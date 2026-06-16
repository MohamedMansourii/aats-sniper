/** @type {import('tailwindcss').Config} */
module.exports = {
  darkMode: ["class"],
  content: ['./index.html', './src/**/*.{js,ts,jsx,tsx}'],
  theme: {
    extend: {
      colors: {
        /* ---- Raw palette tokens (map to CSS hex vars in src/index.css) ---- */
        bg: "var(--bg)",
        panel: "var(--panel)",
        panel2: "var(--panel-2)",
        hover: "var(--hover)",
        text: "var(--text)",
        faint: "var(--faint)",
        /* The single vivid emerald brand/accent color. Use sparingly:
           live / positive / primary CTA. (shadcn's `accent` token is a
           separate subtle hover surface — do not confuse the two.) */
        brand: "var(--accent)",
        up: "var(--up)",
        down: "var(--down)",
        danger: "var(--danger)",
        warn: "var(--warn)",
        info: "var(--info)",
        violet: "var(--violet)",
        "accent-dim": "var(--accent-dim)",

        /* ---- shadcn theme tokens (mapped to the `-h` HSL-channel vars) ---- */
        border: "hsl(var(--border-h))",
        input: "hsl(var(--input-h))",
        ring: "hsl(var(--ring-h))",
        background: "hsl(var(--background-h))",
        foreground: "hsl(var(--foreground-h))",
        primary: {
          DEFAULT: "hsl(var(--primary-h))",
          foreground: "hsl(var(--primary-foreground-h))",
        },
        secondary: {
          DEFAULT: "hsl(var(--secondary-h))",
          foreground: "hsl(var(--secondary-foreground-h))",
        },
        destructive: {
          DEFAULT: "hsl(var(--destructive-h) / <alpha-value>)",
          foreground: "hsl(var(--destructive-foreground-h) / <alpha-value>)",
        },
        muted: {
          DEFAULT: "hsl(var(--muted-h))",
          foreground: "hsl(var(--muted-foreground-h))",
        },
        accent: {
          /* shadcn's subtle hover/selected surface — kept intact so menu,
             dropdown, button hover states stay correct in dark mode. The vivid
             emerald brand color is NOT this token; it is exposed as `brand`
             above (and via var(--accent) directly). */
          DEFAULT: "hsl(var(--accent-h))",
          foreground: "hsl(var(--accent-foreground-h))",
        },
        popover: {
          DEFAULT: "hsl(var(--popover-h))",
          foreground: "hsl(var(--popover-foreground-h))",
        },
        card: {
          DEFAULT: "hsl(var(--card-h))",
          foreground: "hsl(var(--card-foreground-h))",
        },
        sidebar: {
          DEFAULT: "hsl(var(--sidebar-background))",
          foreground: "hsl(var(--sidebar-foreground))",
          primary: "hsl(var(--sidebar-primary))",
          "primary-foreground": "hsl(var(--sidebar-primary-foreground))",
          accent: "hsl(var(--sidebar-accent))",
          "accent-foreground": "hsl(var(--sidebar-accent-foreground))",
          border: "hsl(var(--sidebar-border))",
          ring: "hsl(var(--sidebar-ring))",
        },
      },
      fontFamily: {
        sans: [
          "Inter",
          "ui-sans-serif",
          "system-ui",
          "-apple-system",
          "Segoe UI",
          "Roboto",
          "Helvetica",
          "Arial",
          "sans-serif",
        ],
        mono: [
          "ui-monospace",
          "JetBrains Mono",
          "SF Mono",
          "Menlo",
          "Consolas",
          "monospace",
        ],
      },
      borderRadius: {
        xl: "calc(var(--radius) + 4px)",
        lg: "var(--radius)",
        md: "calc(var(--radius) - 2px)",
        sm: "calc(var(--radius) - 4px)",
        xs: "calc(var(--radius) - 6px)",
      },
      boxShadow: {
        xs: "0 1px 2px 0 rgb(0 0 0 / 0.05)",
        ring: "0 0 0 1px var(--border)",
      },
      keyframes: {
        "accordion-down": {
          from: { height: "0" },
          to: { height: "var(--radix-accordion-content-height)" },
        },
        "accordion-up": {
          from: { height: "var(--radix-accordion-content-height)" },
          to: { height: "0" },
        },
        "caret-blink": {
          "0%,70%,100%": { opacity: "1" },
          "20%,50%": { opacity: "0" },
        },
        "pulse-dot": {
          "0%": { opacity: "1", transform: "scale(1)", boxShadow: "0 0 0 0 currentColor" },
          "70%": { opacity: "0.55", transform: "scale(1)", boxShadow: "0 0 0 4px transparent" },
          "100%": { opacity: "1", transform: "scale(1)", boxShadow: "0 0 0 0 transparent" },
        },
      },
      animation: {
        "accordion-down": "accordion-down 0.2s ease-out",
        "accordion-up": "accordion-up 0.2s ease-out",
        "caret-blink": "caret-blink 1.25s ease-out infinite",
        "pulse-dot": "pulse-dot 1.6s ease-in-out infinite",
      },
    },
  },
  plugins: [require("tailwindcss-animate")],
}

import "./styles.css";

export const metadata = {
  title: "LLM Gateway",
  description: "Operational dashboard for the LLM Gateway demo stack"
};

export default function RootLayout({ children }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}

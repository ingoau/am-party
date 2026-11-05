import { useEffect, useState } from "react";
import QRCode from "qrcode";

export function QrCode({ text, className, label }: { text: string; className?: string; label?: string }) {
  const [svg, setSvg] = useState<string>("");
  useEffect(() => {
    let alive = true;
    QRCode.toString(text, {
      type: "svg",
      margin: 0,
      errorCorrectionLevel: "M",
      color: { dark: "#0b0b10ff", light: "#ffffffff" },
    })
      .then((s) => alive && setSvg(s))
      .catch(() => alive && setSvg(""));
    return () => {
      alive = false;
    };
  }, [text]);
  return <div className={className} role="img" aria-label={label ?? `QR code for ${text}`} dangerouslySetInnerHTML={{ __html: svg }} />;
}

// Bounded validation shared by both 2D feed canvases. The bridge sends raw
// RGB8 or little-endian depth, not JPEG. Bad payloads must not retain old data.
export function decodePixelFrame(frame, bytesPerPixel) {
  if (!frame || !Number.isSafeInteger(frame.width) || !Number.isSafeInteger(frame.height) ||
      frame.width <= 0 || frame.height <= 0 || frame.width > 640 || frame.height > 480 ||
      !Number.isSafeInteger(bytesPerPixel) || bytesPerPixel < 1 || bytesPerPixel > 4 ||
      typeof frame.data !== "string") return null;
  const expected = frame.width * frame.height * bytesPerPixel;
  if (frame.data.length > Math.ceil(expected / 3) * 4 + 4) return null;
  try {
    const bytes = atob(frame.data);
    return bytes.length === expected ? bytes : null;
  } catch { return null; }
}

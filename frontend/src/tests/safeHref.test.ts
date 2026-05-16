import { describe, it, expect } from "vitest";
import { safeHref } from "@/lib/api";

describe("safeHref", () => {
  it("accepts http and https", () => {
    expect(safeHref("https://example.com")).toBe("https://example.com/");
    expect(safeHref("http://example.com/path")).toBe("http://example.com/path");
  });
  it("rejects javascript:", () => {
    expect(safeHref("javascript:alert(1)")).toBeUndefined();
  });
  it("rejects null and non-strings", () => {
    expect(safeHref(null)).toBeUndefined();
    expect(safeHref(undefined)).toBeUndefined();
  });
  it("rejects file: scheme", () => {
    expect(safeHref("file:///etc/passwd")).toBeUndefined();
  });
});

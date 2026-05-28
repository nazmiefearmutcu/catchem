import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { DropZone } from "@/components/DropZone";

// DropZone is a pure (provider-free) component: it owns hover/error state and
// funnels every accepted file through the `onFile` callback. These tests
// exercise the two ingress paths (native <input> change + drag-drop) plus the
// validation gate (extension allow-list + 5 MB cap) and the disabled lockout.

function makeFile(name: string, sizeBytes = 10): File {
  const file = new File(["x"], name, { type: "text/plain" });
  // File.size is read-only and jsdom won't let the constructor blow past the
  // blob content, so define it explicitly when a test needs a specific size.
  Object.defineProperty(file, "size", { value: sizeBytes, configurable: true });
  return file;
}

function getInput(): HTMLInputElement {
  // The canonical control is the visually-hidden <input type="file">.
  return document.querySelector('input[type="file"]') as HTMLInputElement;
}

describe("<DropZone>", () => {
  it("renders the prompt copy, the allow-list hint and an accessible button", () => {
    render(<DropZone onFile={() => {}} />);
    expect(screen.getByText("Drop a file here")).toBeInTheDocument();
    const zone = screen.getByRole("button", { name: /upload article file/i });
    // Enabled zone is focusable.
    expect(zone.getAttribute("tabindex")).toBe("0");
    expect(zone.getAttribute("aria-disabled")).toBe("false");
    // The hint advertises the size cap and at least one allowed extension.
    expect(screen.getByText(/≤ 5 MB/)).toBeInTheDocument();
    expect(screen.getByText(/\.txt/)).toBeInTheDocument();
  });

  it("fires onFile with the selected file via the input change path", () => {
    const onFile = vi.fn();
    render(<DropZone onFile={onFile} />);
    const file = makeFile("article.md");
    fireEvent.change(getInput(), { target: { files: [file] } });
    expect(onFile).toHaveBeenCalledTimes(1);
    expect(onFile).toHaveBeenCalledWith(file);
    // No error rendered for an accepted file.
    expect(screen.queryByRole("alert")).toBeNull();
    // Input value is reset so the same file can be re-selected.
    expect(getInput().value).toBe("");
  });

  it("fires onFile when a valid file is dropped", () => {
    const onFile = vi.fn();
    render(<DropZone onFile={onFile} />);
    const file = makeFile("notes.txt");
    const zone = screen.getByRole("button", { name: /upload article file/i });
    fireEvent.drop(zone, { dataTransfer: { files: [file] } });
    expect(onFile).toHaveBeenCalledTimes(1);
    expect(onFile).toHaveBeenCalledWith(file);
  });

  it("rejects an unsupported extension and surfaces an alert without calling onFile", () => {
    const onFile = vi.fn();
    render(<DropZone onFile={onFile} />);
    fireEvent.change(getInput(), { target: { files: [makeFile("malware.exe")] } });
    expect(onFile).not.toHaveBeenCalled();
    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent(/Unsupported file/i);
  });

  it("rejects a file larger than 5 MB", () => {
    const onFile = vi.fn();
    render(<DropZone onFile={onFile} />);
    const huge = makeFile("big.md", 6 * 1024 * 1024);
    fireEvent.change(getInput(), { target: { files: [huge] } });
    expect(onFile).not.toHaveBeenCalled();
    expect(screen.getByRole("alert")).toHaveTextContent(/too large/i);
  });

  it("clears a prior error once a valid file is accepted", () => {
    const onFile = vi.fn();
    render(<DropZone onFile={onFile} />);
    // First a bad file → error shows.
    fireEvent.change(getInput(), { target: { files: [makeFile("bad.exe")] } });
    expect(screen.getByRole("alert")).toBeInTheDocument();
    // Then a good file → error is cleared and the callback fires.
    fireEvent.change(getInput(), { target: { files: [makeFile("good.json")] } });
    expect(screen.queryByRole("alert")).toBeNull();
    expect(onFile).toHaveBeenCalledWith(expect.objectContaining({ name: "good.json" }));
  });

  it("ignores drops and is non-focusable when disabled", () => {
    const onFile = vi.fn();
    render(<DropZone onFile={onFile} disabled />);
    const zone = screen.getByRole("button", { name: /upload article file/i });
    expect(zone.getAttribute("aria-disabled")).toBe("true");
    expect(zone.getAttribute("tabindex")).toBe("-1");
    fireEvent.drop(zone, { dataTransfer: { files: [makeFile("notes.txt")] } });
    expect(onFile).not.toHaveBeenCalled();
  });

  it("forwards a custom className to the outer wrapper", () => {
    const { container } = render(<DropZone onFile={() => {}} className="my-zone" />);
    expect((container.firstChild as HTMLElement).className).toContain("my-zone");
  });
});

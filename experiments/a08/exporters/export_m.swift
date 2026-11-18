// Family M: macOS PDF export through the system print path (Quartz PDFContext), no UI automation.
//   M1, M2, M4: the .docx read by AppKit (NSAttributedString, .officeOpenXML, as TextEdit opens it) in an NSTextView,
//               printed with NSPrintOperation(jobDisposition = .save) — TextEdit's "Export as PDF"/"Save as PDF" path.
//   M3:         the HTML read by WKWebView and printed with WKWebView.printOperation — Safari's print path.
// Fonts are registered for this process only (CTFontManagerRegisterFontsForURL, scope .process).
// usage: swift exporters/export_m.swift <fonts_dir> <sources_dir> <out_dir>
import AppKit
import WebKit

let args = CommandLine.arguments
let cwd = URL(fileURLWithPath: FileManager.default.currentDirectoryPath)
let fontsDir = URL(fileURLWithPath: args[1], relativeTo: cwd).absoluteURL, src = URL(fileURLWithPath: args[2], relativeTo: cwd).absoluteURL, out = URL(fileURLWithPath: args[3], relativeTo: cwd).absoluteURL
try? FileManager.default.createDirectory(at: out, withIntermediateDirectories: true)
for f in (try? FileManager.default.contentsOfDirectory(at: fontsDir, includingPropertiesForKeys: nil)) ?? [] where f.pathExtension == "ttf" {
    var err: Unmanaged<CFError>?
    CTFontManagerRegisterFontsForURL(f as CFURL, .process, &err)
}
let app = NSApplication.shared
app.setActivationPolicy(.prohibited)
let mm: CGFloat = 72.0 / 25.4
let a4 = NSSize(width: 210 * mm, height: 297 * mm)

func printInfo(_ dest: URL) -> NSPrintInfo {
    let pi = NSPrintInfo()
    pi.paperSize = a4
    pi.topMargin = 25 * mm; pi.bottomMargin = 25 * mm; pi.leftMargin = 25 * mm; pi.rightMargin = 25 * mm
    pi.horizontalPagination = .fit
    pi.verticalPagination = .automatic
    pi.isHorizontallyCentered = false
    pi.isVerticallyCentered = false
    pi.jobDisposition = .save
    pi.dictionary()[NSPrintInfo.AttributeKey.jobSavingURL] = dest
    pi.dictionary()[NSPrintInfo.AttributeKey.headerAndFooter] = false
    return pi
}

// TextEdit "wrap to page" layout: one NSTextContainer per page sharing an NSLayoutManager; a form feed (the docx page
// break, as AppKit imports it) moves the text to the next container.
final class PagedView: NSView {
    var pages = 1
    override var isFlipped: Bool { true }
    override func knowsPageRange(_ range: NSRangePointer) -> Bool { range.pointee = NSRange(location: 1, length: pages); return true }
    override func rectForPage(_ page: Int) -> NSRect { NSRect(x: 0, y: CGFloat(page - 1) * a4.height, width: a4.width, height: a4.height) }
}

func exportDocx(_ name: String, _ dest: URL) throws {
    let data = try Data(contentsOf: src.appendingPathComponent(name))
    let s = try NSAttributedString(data: data, options: [.documentType: NSAttributedString.DocumentType.officeOpenXML], documentAttributes: nil)
    let storage = NSTextStorage(attributedString: s)
    let lm = NSLayoutManager()
    storage.addLayoutManager(lm)
    let content = NSSize(width: a4.width - 50 * mm, height: a4.height - 50 * mm)
    let view = PagedView(frame: .zero)
    var n = 0
    while true {
        let tc = NSTextContainer(size: content)
        tc.lineFragmentPadding = 0
        lm.addTextContainer(tc)
        let tv = NSTextView(frame: NSRect(x: 25 * mm, y: CGFloat(n) * a4.height + 25 * mm, width: content.width, height: content.height), textContainer: tc)
        tv.textContainerInset = .zero
        view.addSubview(tv)
        n += 1
        let r = lm.glyphRange(for: tc)
        if NSMaxRange(r) >= lm.numberOfGlyphs || n > 50 { break }
    }
    view.pages = n
    view.frame = NSRect(x: 0, y: 0, width: a4.width, height: CGFloat(n) * a4.height)
    let pi = printInfo(dest)
    pi.topMargin = 0; pi.bottomMargin = 0; pi.leftMargin = 0; pi.rightMargin = 0
    let op = NSPrintOperation(view: view, printInfo: pi)
    op.showsPrintPanel = false
    op.showsProgressPanel = false
    if !op.run() { throw NSError(domain: "export", code: 1) }
}

final class Loader: NSObject, WKNavigationDelegate {
    var done = false
    func webView(_ w: WKWebView, didFinish n: WKNavigation!) { done = true }
}

func exportHtml(_ name: String, _ dest: URL) {
    let cfg = WKWebViewConfiguration()
    let wv = WKWebView(frame: NSRect(x: 0, y: 0, width: a4.width, height: a4.height), configuration: cfg)
    let win = NSWindow(contentRect: wv.frame, styleMask: [.borderless], backing: .buffered, defer: false)
    win.contentView = wv
    let l = Loader()
    wv.navigationDelegate = l
    let file = src.appendingPathComponent(name)
    wv.loadFileURL(file, allowingReadAccessTo: src.deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent())
    while !l.done { RunLoop.main.run(until: Date(timeIntervalSinceNow: 0.05)) }
    RunLoop.main.run(until: Date(timeIntervalSinceNow: 1.0))
    let pi = printInfo(dest)
    pi.topMargin = 0; pi.bottomMargin = 0; pi.leftMargin = 0; pi.rightMargin = 0
    let op = wv.printOperation(with: pi)
    op.showsPrintPanel = false
    op.showsProgressPanel = false
    op.view?.frame = wv.bounds
    var finished = false
    op.runModal(for: win, delegate: nil, didRun: nil, contextInfo: nil)
    for _ in 0..<200 where !finished {
        RunLoop.main.run(until: Date(timeIntervalSinceNow: 0.05))
        finished = FileManager.default.fileExists(atPath: dest.path)
    }
    RunLoop.main.run(until: Date(timeIntervalSinceNow: 1.0))
}

for (i, name) in [(1, "T1.docx"), (2, "T2.docx"), (4, "T4.docx")] {
    let dest = out.appendingPathComponent("M\(i).pdf")
    try? FileManager.default.removeItem(at: dest)
    do { try exportDocx(name, dest) } catch { print("M\(i) failed: \(error)") }
}
let d3 = out.appendingPathComponent("M3.pdf")
try? FileManager.default.removeItem(at: d3)
exportHtml("html/T3.html", d3)
print(ProcessInfo.processInfo.operatingSystemVersionString)

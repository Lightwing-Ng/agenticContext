/* Code version: v1.0.0-codex.0 */

(function bootstrapScrollableDataTables(globalScope) {
    "use strict";

    const HEADER_HEIGHT_PROPERTY = "--scrollable-data-table-header-height";
    const SCROLLBAR_WIDTH_PROPERTY = "--scrollable-data-table-scrollbar-width";

    function directChild(shell, selector) {
        return Array.from(shell?.children || []).find((child) => child.matches?.(selector)) || null;
    }

    function tableParts(shell) {
        const header = directChild(shell, "table[data-table-header]");
        const scroll = directChild(shell, "[data-table-scroll]");
        const body = scroll?.querySelector("table[data-table-body]") || null;
        return {header, scroll, body};
    }

    function measurementRow(table, expectedColumnCount) {
        return Array.from(table?.rows || []).find((row) => {
            if (row.hidden || row.matches("[data-table-empty-row], [data-table-summary-row]")) return false;
            if (globalScope.getComputedStyle?.(row).display === "none") return false;
            return row.cells.length === expectedColumnCount
                && Array.from(row.cells).every((cell) => cell.colSpan === 1);
        }) || null;
    }

    function setProperty(element, name, value) {
        if (element?.style.getPropertyValue(name) !== value) element?.style.setProperty(name, value);
    }

    function syncColumnWidths(header, body, scrollbarWidth) {
        const headerRow = Array.from(header?.rows || []).find((row) => (
            Array.from(row.cells).every((cell) => cell.colSpan === 1)
        ));
        if (!headerRow) return;
        const bodyRow = measurementRow(body, headerRow.cells.length);
        if (!bodyRow) return;
        const widths = Array.from(bodyRow.cells, (cell) => cell.getBoundingClientRect().width);
        if (widths.length) widths[widths.length - 1] += scrollbarWidth;
        Array.from(headerRow.cells).forEach((cell, index) => {
            cell.style.width = `${Math.max(1, widths[index] || 1)}px`;
        });
        header.style.width = `${Math.max(body.scrollWidth, body.getBoundingClientRect().width)}px`;
    }

    function attach(shell) {
        if (!(shell instanceof HTMLElement)) return () => {};
        let frameId = 0;
        let resizeObserver = null;
        const {header, scroll, body} = tableParts(shell);
        if (!(header instanceof HTMLTableElement)
            || !(scroll instanceof HTMLElement)
            || !(body instanceof HTMLTableElement)) return () => {};

        function syncHorizontalPosition() {
            header.style.translate = scroll.scrollLeft ? `${-scroll.scrollLeft}px 0` : "";
        }

        function sync() {
            frameId = 0;
            const scrollbarWidth = Math.max(0, scroll.offsetWidth - scroll.clientWidth);
            setProperty(shell, SCROLLBAR_WIDTH_PROPERTY, `${scrollbarWidth}px`);
            syncColumnWidths(header, body, scrollbarWidth);
            const height = header.getBoundingClientRect().height;
            if (height > 0) setProperty(shell, HEADER_HEIGHT_PROPERTY, `${Math.ceil(height)}px`);
            syncHorizontalPosition();
        }

        function schedule() {
            if (frameId) globalScope.cancelAnimationFrame(frameId);
            frameId = globalScope.requestAnimationFrame(sync);
        }

        scroll.addEventListener("scroll", syncHorizontalPosition, {passive: true});
        globalScope.addEventListener("resize", schedule);
        if (typeof ResizeObserver === "function") {
            resizeObserver = new ResizeObserver(schedule);
            [shell, header, scroll, body].forEach((node) => resizeObserver.observe(node));
        }
        schedule();

        return () => {
            if (frameId) globalScope.cancelAnimationFrame(frameId);
            scroll.removeEventListener("scroll", syncHorizontalPosition);
            globalScope.removeEventListener("resize", schedule);
            resizeObserver?.disconnect();
            header.style.removeProperty("translate");
            shell.style.removeProperty(HEADER_HEIGHT_PROPERTY);
            shell.style.removeProperty(SCROLLBAR_WIDTH_PROPERTY);
        };
    }

    function attachAll(root = document) {
        const attached = new Map();
        function reconcile() {
            const shells = new Set(root.querySelectorAll(".scrollable-data-table-shell"));
            shells.forEach((shell) => {
                if (!attached.has(shell)) attached.set(shell, attach(shell));
            });
            Array.from(attached).forEach(([shell, cleanup]) => {
                if (shell.isConnected && shells.has(shell)) return;
                cleanup();
                attached.delete(shell);
            });
        }
        reconcile();
        const observer = typeof MutationObserver === "function" ? new MutationObserver(reconcile) : null;
        observer?.observe(root, {childList: true, subtree: true});
        return () => {
            observer?.disconnect();
            attached.forEach((cleanup) => cleanup());
            attached.clear();
        };
    }

    const api = Object.freeze({attach, attachAll, measurementRow, syncColumnWidths});
    globalScope.SHARED_SCROLLABLE_DATA_TABLE = api;

    if (typeof document !== "undefined") {
        const initialize = () => attachAll(document);
        if (document.readyState === "loading") {
            document.addEventListener("DOMContentLoaded", initialize, {once: true});
        } else {
            initialize();
        }
    }
}(typeof window !== "undefined" ? window : globalThis));

/* Code version: v1.0.0-codex.0 */

(() => {
    "use strict";

    const { full, compact, months } = JSON.parse(document.getElementById("date_display_state").textContent);
    const iso = /^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.(\d+))?)?(Z|[+-]\d{2}:?\d{2})?)?$/i;
    const timeZones = new Map();
    const pad = (number, length = 2) => String(number).padStart(length, "0");

    function zonedParts(stamp, timeZone) {
        if (!timeZones.has(timeZone)) {
            timeZones.set(timeZone, new Intl.DateTimeFormat("en-US", {
                timeZone,
                calendar: "gregory",
                numberingSystem: "latn",
                year: "numeric",
                month: "numeric",
                day: "numeric",
                hour: "numeric",
                minute: "numeric",
                second: "numeric",
                hourCycle: "h23",
            }));
        }
        const fields = {};
        for (const { type, value } of timeZones.get(timeZone).formatToParts(stamp)) {
            if (type !== "literal") fields[type] = Number(value);
        }
        return fields;
    }

    function localParts(stamp) {
        return {
            year: stamp.getFullYear(),
            month: stamp.getMonth() + 1,
            day: stamp.getDate(),
            hour: stamp.getHours(),
            minute: stamp.getMinutes(),
            second: stamp.getSeconds(),
        };
    }

    function parse(value, { timeZone, wallTime }) {
        if (value instanceof Date || typeof value === "number") {
            const stamp = new Date(value);
            if (!Number.isFinite(stamp.getTime())) return null;
            return { ...(timeZone ? zonedParts(stamp, timeZone) : localParts(stamp)), precision: "second" };
        }
        if (typeof value !== "string") return null;
        const match = iso.exec(value.trim());
        if (!match) return null;
        const [, year, month, day, hour, minute, second, fraction, zone] = match;
        const clock = hour === undefined ? "" : `T${hour}:${minute}:${second ?? "00"}`;
        const milliseconds = fraction ? `.${(fraction + "000").slice(0, 3)}` : "";
        const dayKey = `${year}-${month}-${day}`;
        const wall = new Date(`${dayKey}${clock || "T00:00:00"}Z`);
        if (!Number.isFinite(wall.getTime()) || wall.toISOString().slice(0, 10) !== dayKey) {
            return null;
        }
        const precision = second !== undefined ? "second" : clock ? "minute" : "day";
        if (clock && (timeZone || (zone && !wallTime))) {
            const stamp = new Date(`${dayKey}${clock}${milliseconds}${zone || "Z"}`);
            if (!Number.isFinite(stamp.getTime())) return null;
            return { ...(timeZone ? zonedParts(stamp, timeZone) : localParts(stamp)), precision };
        }
        return {
            year: Number(year),
            month: Number(month),
            day: Number(day),
            hour: Number(hour),
            minute: Number(minute),
            second: Number(second ?? "0"),
            precision,
        };
    }

    function dateLabel({ year, month, day }, compactDisplay) {
        const terms = { yyyy: pad(year, 4), mmm: months[month - 1], mm: pad(month), d: day, dd: pad(day) };
        return (compactDisplay ? compact : full).replace(/\{(\w+)\}/g, (_, term) => terms[term]);
    }

    function timeLabel(parts, seconds) {
        const clock = `${pad(parts.hour)}:${pad(parts.minute)}`;
        return (seconds ?? parts.precision === "second") ? `${clock}:${pad(parts.second)}` : clock;
    }

    function fallback(value, options) {
        return options.fallback ?? (typeof value === "string" ? value : "");
    }

    window.APP_DATES = Object.freeze({
        date(value, options = {}) {
            const parts = parse(value, options);
            return parts ? dateLabel(parts, options.compact) : fallback(value, options);
        },
        dateTime(value, options = {}) {
            const parts = parse(value, options);
            if (!parts) return fallback(value, options);
            const day = dateLabel(parts, options.compact);
            return parts.precision === "day" ? day : `${day} ${timeLabel(parts, options.seconds)}`;
        },
        time(value, options = {}) {
            const parts = parse(value, options);
            return parts && parts.precision !== "day" ? timeLabel(parts, options.seconds) : fallback(value, options);
        },
    });
})();

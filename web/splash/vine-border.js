/**
 * 3D 藤蔓缠绕边框 — 精致手绘 SVG
 * 四角多层卷须 + 边框藤蔓延伸 + 叶子 + 花苞 + 萤火虫
 */

(function () {
    'use strict';
    const SVG_NS = 'http://www.w3.org/2000/svg';

    function el(tag, attrs) {
        const e = document.createElementNS(SVG_NS, tag);
        for (const k in attrs) e.setAttribute(k, attrs[k]);
        return e;
    }

    /**
     * 精致叶子（水滴形 + 叶脉）
     */
    function leaf(cx, cy, angle, size, delay) {
        const g = document.createElementNS(SVG_NS, 'g');
        g.setAttribute('class', 'leaf-in');
        g.setAttribute('transform', `translate(${cx},${cy}) rotate(${angle})`);
        g.style.animationDelay = `${delay}s, ${4 + delay}s`;
        g.appendChild(el('path', {
            d: `M 0 0 Q ${size * 0.5} ${-size * 0.45} ${size} 0 Q ${size * 0.5} ${size * 0.45} 0 0 Z`,
            class: 'leaf-fill',
        }));
        g.appendChild(el('line', {
            x1: 0, y1: 0, x2: size, y2: 0,
            stroke: 'rgba(143,229,96,0.3)', 'stroke-width': '0.5',
        }));
        return g;
    }

    /**
     * 花苞（小椭圆 + 茎）
     */
    function bud(cx, cy, angle, size, delay) {
        const g = document.createElementNS(SVG_NS, 'g');
        g.setAttribute('class', 'bud-in');
        g.setAttribute('transform', `translate(${cx},${cy}) rotate(${angle})`);
        g.style.animationDelay = `${delay}s, ${4 + delay}s`;
        g.appendChild(el('ellipse', {
            cx: 0, cy: 0, rx: size * 0.6, ry: size,
            class: 'bud-fill',
        }));
        g.appendChild(el('line', {
            x1: 0, y1: 0, x2: -size * 1.2, y2: 0,
            stroke: 'rgba(143,229,96,0.35)', 'stroke-width': '0.8',
        }));
        return g;
    }

    /**
     * 萤火虫
     */
    function spark(cx, cy, r, delay) {
        const c = el('circle', { cx, cy, r, class: 'spark' });
        c.style.animationDelay = `${delay}s`;
        return c;
    }

    function build() {
        const container = document.querySelector('.vine-frame');
        if (!container) return;
        const svg = container.querySelector('svg');
        if (!svg) return;

        const W = window.innerWidth;
        const H = window.innerHeight;
        const m = 16;
        const cs = 100; // 角落藤蔓范围

        svg.innerHTML = '';
        svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
        svg.setAttribute('preserveAspectRatio', 'none');

        // --- 内层虚线边框 ---
        svg.appendChild(el('rect', {
            x: m, y: m, width: W - m * 2, height: H - m * 2,
            rx: 6, class: 'frame-line',
        }));

        // --- 四角藤蔓（每角多层卷须） ---
        // 螺旋卷须生成器：从起点画一条优美的螺旋曲线
        function tendril(x, y, startAngle, length, turns, direction) {
            let d = `M ${x} ${y}`;
            const steps = 40;
            let cx = x, cy = y;
            let angle = startAngle;
            for (let i = 1; i <= steps; i++) {
                const t = i / steps;
                const r = length * (1 - t) * 0.15 + 2;
                angle += (direction * 10 * (1 - t * 0.5));
                cx += Math.cos(angle * Math.PI / 180) * (length / steps);
                cy += Math.sin(angle * Math.PI / 180) * (length / steps);
                d += ` L ${cx.toFixed(1)} ${cy.toFixed(1)}`;
            }
            return d;
        }

        const delayBase = 0.3;

        // === 左上角 ===
        const tl = m;
        // 主藤蔓：从左边中部向上卷曲到角，再向右延伸
        svg.appendChild(el('path', {
            d: `M ${m} ${m + cs}
                C ${m} ${m + cs * 0.5}, ${m + 5} ${m + cs * 0.15}, ${m + 30} ${m + 8}
                C ${m + 50} ${m + 2}, ${m + 70} ${m + 5}, ${m + cs} ${m + 15}`,
            class: 'vine-path grow',
        })).style.animationDelay = `${delayBase}s`;

        // 螺旋卷须 1
        svg.appendChild(el('path', {
            d: `M ${m + 30} ${m + 8}
                Q ${m + 20} ${m - 5}, ${m + 15} ${m - 15}
                Q ${m + 12} ${m - 25}, ${m + 20} ${m - 30}
                Q ${m + 28} ${m - 32}, ${m + 32} ${m - 25}`,
            class: 'vine-branch grow',
        })).style.animationDelay = `${delayBase + 0.2}s`;

        // 螺旋卷须 2（更小）
        svg.appendChild(el('path', {
            d: `M ${m + 60} ${m + 3}
                Q ${m + 55} ${m - 8}, ${m + 52} ${m - 18}
                Q ${m + 50} ${m - 25}, ${m + 56} ${m - 28}`,
            class: 'vine-whisper grow',
        })).style.animationDelay = `${delayBase + 0.4}s`;

        // 细枝向下延伸
        svg.appendChild(el('path', {
            d: `M ${m + 40} ${m + 5} Q ${m + 45} ${m + 30}, ${m + 38} ${m + 50}`,
            class: 'vine-branch grow',
        })).style.animationDelay = `${delayBase + 0.3}s`;

        // 叶子
        [
            { x: m + 20, y: m + 12, a: 35, s: 9, d: 1.5 },
            { x: m + 55, y: m + 5, a: -15, s: 8, d: 1.7 },
            { x: m + 18, y: m - 18, a: 60, s: 6, d: 1.9 },
            { x: m + 56, y: m - 22, a: 50, s: 5, d: 2.1 },
            { x: m + 42, y: m + 35, a: 80, s: 7, d: 2.0 },
            { x: m + 36, y: m + 52, a: 95, s: 6, d: 2.2 },
        ].forEach(lf => svg.appendChild(leaf(lf.x, lf.y, lf.a, lf.s, lf.d)));

        // 花苞
        svg.appendChild(bud(m + 70, m + 8, -30, 4, 2.3));
        svg.appendChild(bud(m + 15, m - 28, 45, 3.5, 2.5));

        // === 右上角 ===
        svg.appendChild(el('path', {
            d: `M ${W - m - cs} ${m + 15}
                C ${W - m - 70} ${m + 5}, ${W - m - 50} ${m + 2}, ${W - m - 30} ${m + 8}
                C ${W - m - 5} ${m + 15}, ${W - m} ${m + cs * 0.5}, ${W - m} ${m + cs}`,
            class: 'vine-path grow',
        })).style.animationDelay = `${delayBase + 0.3}s`;

        svg.appendChild(el('path', {
            d: `M ${W - m - 30} ${m + 8}
                Q ${W - m - 20} ${m - 5}, ${W - m - 15} ${m - 15}
                Q ${W - m - 12} ${m - 25}, ${W - m - 20} ${m - 30}
                Q ${W - m - 28} ${m - 32}, ${W - m - 32} ${m - 25}`,
            class: 'vine-branch grow',
        })).style.animationDelay = `${delayBase + 0.5}s`;

        svg.appendChild(el('path', {
            d: `M ${W - m - 60} ${m + 3}
                Q ${W - m - 55} ${m - 8}, ${W - m - 52} ${m - 18}
                Q ${W - m - 50} ${m - 25}, ${W - m - 56} ${m - 28}`,
            class: 'vine-whisper grow',
        })).style.animationDelay = `${delayBase + 0.7}s`;

        svg.appendChild(el('path', {
            d: `M ${W - m - 40} ${m + 5} Q ${W - m - 45} ${m + 30}, ${W - m - 38} ${m + 50}`,
            class: 'vine-branch grow',
        })).style.animationDelay = `${delayBase + 0.6}s`;

        [
            { x: W - m - 20, y: m + 12, a: 145, s: 9, d: 1.8 },
            { x: W - m - 55, y: m + 5, a: 195, s: 8, d: 2.0 },
            { x: W - m - 18, y: m - 18, a: 120, s: 6, d: 2.2 },
            { x: W - m - 56, y: m - 22, a: 130, s: 5, d: 2.4 },
            { x: W - m - 42, y: m + 35, a: 100, s: 7, d: 2.3 },
            { x: W - m - 36, y: m + 52, a: 85, s: 6, d: 2.5 },
        ].forEach(lf => svg.appendChild(leaf(lf.x, lf.y, lf.a, lf.s, lf.d)));

        svg.appendChild(bud(W - m - 70, m + 8, 210, 4, 2.6));
        svg.appendChild(bud(W - m - 15, m - 28, 135, 3.5, 2.8));

        // === 左下角 ===
        svg.appendChild(el('path', {
            d: `M ${m} ${H - m - cs}
                C ${m} ${H - m - cs * 0.5}, ${m + 5} ${H - m - cs * 0.15}, ${m + 30} ${H - m - 8}
                C ${m + 50} ${H - m - 2}, ${m + 70} ${H - m - 5}, ${m + cs} ${H - m - 15}`,
            class: 'vine-path grow',
        })).style.animationDelay = `${delayBase + 0.6}s`;

        svg.appendChild(el('path', {
            d: `M ${m + 30} ${H - m - 8}
                Q ${m + 20} ${H - m + 5}, ${m + 15} ${H - m + 15}
                Q ${m + 12} ${H - m + 25}, ${m + 20} ${H - m + 30}
                Q ${m + 28} ${H - m + 32}, ${m + 32} ${H - m + 25}`,
            class: 'vine-branch grow',
        })).style.animationDelay = `${delayBase + 0.8}s`;

        svg.appendChild(el('path', {
            d: `M ${m + 60} ${H - m - 3}
                Q ${m + 55} ${H - m + 8}, ${m + 52} ${H - m + 18}
                Q ${m + 50} ${H - m + 25}, ${m + 56} ${H - m + 28}`,
            class: 'vine-whisper grow',
        })).style.animationDelay = `${delayBase + 1.0}s`;

        svg.appendChild(el('path', {
            d: `M ${m + 40} ${H - m - 5} Q ${m + 45} ${H - m - 30}, ${m + 38} ${H - m - 50}`,
            class: 'vine-branch grow',
        })).style.animationDelay = `${delayBase + 0.9}s`;

        [
            { x: m + 20, y: H - m - 12, a: -35, s: 9, d: 2.1 },
            { x: m + 55, y: H - m - 5, a: 15, s: 8, d: 2.3 },
            { x: m + 18, y: H - m + 18, a: -60, s: 6, d: 2.5 },
            { x: m + 56, y: H - m + 22, a: -50, s: 5, d: 2.7 },
            { x: m + 42, y: H - m - 35, a: -80, s: 7, d: 2.6 },
            { x: m + 36, y: H - m - 52, a: -95, s: 6, d: 2.8 },
        ].forEach(lf => svg.appendChild(leaf(lf.x, lf.y, lf.a, lf.s, lf.d)));

        svg.appendChild(bud(m + 70, H - m - 8, 30, 4, 2.9));
        svg.appendChild(bud(m + 15, H - m + 28, -45, 3.5, 3.1));

        // === 右下角 ===
        svg.appendChild(el('path', {
            d: `M ${W - m - cs} ${H - m - 15}
                C ${W - m - 70} ${H - m - 5}, ${W - m - 50} ${H - m - 2}, ${W - m - 30} ${H - m - 8}
                C ${W - m - 5} ${H - m - 15}, ${W - m} ${H - m - cs * 0.5}, ${W - m} ${H - m - cs}`,
            class: 'vine-path grow',
        })).style.animationDelay = `${delayBase + 0.9}s`;

        svg.appendChild(el('path', {
            d: `M ${W - m - 30} ${H - m - 8}
                Q ${W - m - 20} ${H - m + 5}, ${W - m - 15} ${H - m + 15}
                Q ${W - m - 12} ${H - m + 25}, ${W - m - 20} ${H - m + 30}
                Q ${W - m - 28} ${H - m + 32}, ${W - m - 32} ${H - m + 25}`,
            class: 'vine-branch grow',
        })).style.animationDelay = `${delayBase + 1.1}s`;

        svg.appendChild(el('path', {
            d: `M ${W - m - 60} ${H - m - 3}
                Q ${W - m - 55} ${H - m + 8}, ${W - m - 52} ${H - m + 18}
                Q ${W - m - 50} ${H - m + 25}, ${W - m - 56} ${H - m + 28}`,
            class: 'vine-whisper grow',
        })).style.animationDelay = `${delayBase + 1.3}s`;

        svg.appendChild(el('path', {
            d: `M ${W - m - 40} ${H - m - 5} Q ${W - m - 45} ${H - m - 30}, ${W - m - 38} ${H - m - 50}`,
            class: 'vine-branch grow',
        })).style.animationDelay = `${delayBase + 1.2}s`;

        [
            { x: W - m - 20, y: H - m - 12, a: -145, s: 9, d: 2.4 },
            { x: W - m - 55, y: H - m - 5, a: -195, s: 8, d: 2.6 },
            { x: W - m - 18, y: H - m + 18, a: -120, s: 6, d: 2.8 },
            { x: W - m - 56, y: H - m + 22, a: -130, s: 5, d: 3.0 },
            { x: W - m - 42, y: H - m - 35, a: -100, s: 7, d: 2.9 },
            { x: W - m - 36, y: H - m - 52, a: -85, s: 6, d: 3.1 },
        ].forEach(lf => svg.appendChild(leaf(lf.x, lf.y, lf.a, lf.s, lf.d)));

        svg.appendChild(bud(W - m - 70, H - m - 8, -30, 4, 3.2));
        svg.appendChild(bud(W - m - 15, H - m + 28, 45, 3.5, 3.4));

        // --- 边框中段藤蔓延伸 ---
        // 上边中段
        svg.appendChild(el('path', {
            d: `M ${W * 0.35} ${m} Q ${W * 0.4} ${m - 10}, ${W * 0.45} ${m - 6} Q ${W * 0.5} ${m - 12}, ${W * 0.55} ${m - 4} Q ${W * 0.6} ${m - 10}, ${W * 0.65} ${m}`,
            class: 'vine-whisper grow',
        })).style.animationDelay = `${delayBase + 1.5}s`;

        // 下边中段
        svg.appendChild(el('path', {
            d: `M ${W * 0.35} ${H - m} Q ${W * 0.4} ${H - m + 10}, ${W * 0.45} ${H - m + 6} Q ${W * 0.5} ${H - m + 12}, ${W * 0.55} ${H - m + 4} Q ${W * 0.6} ${H - m + 10}, ${W * 0.65} ${H - m}`,
            class: 'vine-whisper grow',
        })).style.animationDelay = `${delayBase + 1.7}s`;

        // 左边中段
        svg.appendChild(el('path', {
            d: `M ${m} ${H * 0.35} Q ${m - 10} ${H * 0.4}, ${m - 6} ${H * 0.45} Q ${m - 12} ${H * 0.5}, ${m - 4} ${H * 0.55} Q ${m - 10} ${H * 0.6}, ${m} ${H * 0.65}`,
            class: 'vine-whisper grow',
        })).style.animationDelay = `${delayBase + 1.6}s`;

        // 右边中段
        svg.appendChild(el('path', {
            d: `M ${W - m} ${H * 0.35} Q ${W - m + 10} ${H * 0.4}, ${W - m + 6} ${H * 0.45} Q ${W - m + 12} ${H * 0.5}, ${W - m + 4} ${H * 0.55} Q ${W - m + 10} ${H * 0.6}, ${W - m} ${H * 0.65}`,
            class: 'vine-whisper grow',
        })).style.animationDelay = `${delayBase + 1.8}s`;

        // --- 边框中点叶子 ---
        [
            { x: W * 0.5, y: m - 8, a: 90, s: 7, d: 3.0 },
            { x: W * 0.5, y: H - m + 8, a: -90, s: 7, d: 3.2 },
            { x: m - 8, y: H * 0.5, a: 0, s: 7, d: 3.1 },
            { x: W - m + 8, y: H * 0.5, a: 180, s: 7, d: 3.3 },
        ].forEach(lf => svg.appendChild(leaf(lf.x, lf.y, lf.a, lf.s, lf.d)));

        // --- 萤火虫 ---
        const sparks = [
            { x: W * 0.2, y: m + 15, r: 1.5, d: 2 },
            { x: W * 0.8, y: m + 15, r: 1.2, d: 2.5 },
            { x: m + 15, y: H * 0.25, r: 1.5, d: 3 },
            { x: W - m - 15, y: H * 0.25, r: 1.2, d: 3.5 },
            { x: m + 15, y: H * 0.75, r: 1.5, d: 4 },
            { x: W - m - 15, y: H * 0.75, r: 1.2, d: 4.5 },
            { x: W * 0.3, y: H - m - 15, r: 1.5, d: 5 },
            { x: W * 0.7, y: H - m - 15, r: 1.2, d: 5.5 },
            { x: W * 0.5, y: m - 15, r: 1.8, d: 6 },
            { x: W * 0.5, y: H - m + 15, r: 1.5, d: 6.5 },
            { x: m - 15, y: H * 0.5, r: 1.5, d: 7 },
            { x: W - m + 15, y: H * 0.5, r: 1.8, d: 7.5 },
        ];
        sparks.forEach(s => svg.appendChild(spark(s.x, s.y, s.r, s.d)));
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', build);
    } else {
        build();
    }

    let timer;
    window.addEventListener('resize', () => {
        clearTimeout(timer);
        timer = setTimeout(build, 300);
    });
})();

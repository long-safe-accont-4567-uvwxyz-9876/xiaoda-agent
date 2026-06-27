// ==================== 全屏 WebGL Shader（渐变背景 + 粒子 + 波浪 + 极光） ====================
// 参考 Obsidian MCP: SiriWave wave 变体 + GlowHorizon 光晕 + 宇宙星图粒子
const canvas = document.getElementById('bg-canvas');
const gl = canvas.getContext('webgl', { alpha: false, premultipliedAlpha: false })
    || canvas.getContext('experimental-webgl', { alpha: false, premultipliedAlpha: false });

let animationId = null;

function resizeCanvas() {
    const w = window.innerWidth;
    const h = window.innerHeight;
    const dpr = Math.min(window.devicePixelRatio || 1, 1);
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
    canvas.style.width = w + 'px';
    canvas.style.height = h + 'px';
    if (gl) gl.viewport(0, 0, canvas.width, canvas.height);
}

if (gl) {
    const vsSource = `
        attribute vec2 aPos;
        void main() {
            gl_Position = vec4(aPos, 0.0, 1.0);
        }
    `;

    // 全屏 fragment shader：渐变背景 + 极光底晕 + 波浪线 + 粒子（性能优化版）
    const fsSource = `
        precision mediump float;
        uniform vec2 iResolution;
        uniform float iTime;

        float hash(vec2 p) {
            return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453);
        }

        void main() {
            vec2 uv = gl_FragCoord.xy / iResolution;
            float t = iTime;
            float aspect = iResolution.x / iResolution.y;

            // === 1. 渐变背景 ===
            vec3 bgTop = vec3(0.051, 0.078, 0.059);
            vec3 bgBot = vec3(0.098, 0.165, 0.122);
            vec3 bg = mix(bgBot, bgTop, uv.y);

            // === 2. 极光底晕（2层，简化） ===
            float a1 = smoothstep(0.0, 0.45, uv.y);
            a1 = 1.0 - a1;
            a1 = a1 * a1;
            bg += vec3(0.04, 0.10, 0.05) * a1;

            float a2 = smoothstep(0.0, 0.15, uv.y);
            a2 = 1.0 - a2;
            a2 = a2 * a2 * a2;
            bg += vec3(0.08, 0.16, 0.06) * a2;

            float pulse = sin(t * 0.5) * 0.5 + 0.5;
            bg += vec3(0.01, 0.02, 0.01) * a1 * pulse;

            // === 3. 波浪线（简化为2层，去掉noise） ===
            float wx = uv.x * aspect;
            float waveY = 0.15 + sin(wx * 12.0 + t * 1.5) * 0.06 + sin(wx * 20.0 - t * 2.0) * 0.035;

            float dw = uv.y - waveY;
            float waveGlow = 0.008 / (abs(dw) + 0.003);
            waveGlow = clamp(waveGlow, 0.0, 8.0);

            float waveFill = smoothstep(waveY - 0.05, waveY, uv.y);
            waveFill = 1.0 - waveFill;
            waveFill *= waveFill * 0.15;
            bg += vec3(0.20, 0.50, 0.15) * waveFill;
            bg += vec3(0.25, 0.55, 0.15) * waveGlow * 0.3;

            // === 4. 浮动粒子（25个，大幅减少） ===
            for (int i = 0; i < 25; i++) {
                float fi = float(i);
                float px = hash(vec2(fi * 0.17, 0.31));
                float py = hash(vec2(fi * 0.23, 0.67));

                float speed = 0.02 + hash(vec2(fi, 0.0)) * 0.04;
                float drift = sin(t * speed + fi) * 0.08;
                float rise = t * (0.003 + hash(vec2(fi, 1.0)) * 0.005);

                float fy = mod(py + rise, 1.2) - 0.1;
                float fx = mod(px + drift + sin(t * 0.3 + fi * 0.5) * 0.03, 1.0);

                float size = 0.001 + hash(vec2(fi, 2.0)) * 0.003;
                float brightness = 0.3 + hash(vec2(fi, 3.0)) * 0.5;
                float twinkle = sin(t * (2.0 + hash(vec2(fi, 4.0)) * 3.0) + fi * 6.28) * 0.3 + 0.7;

                vec2 pPos = vec2(fx, fy);
                float dist = length((uv - pPos) * vec2(aspect, 1.0));
                float particleAlpha = smoothstep(size * 2.0, 0.0, dist) * brightness * twinkle;

                vec3 pColor = mix(
                    vec3(0.35, 0.75, 0.25),
                    vec3(0.7, 0.95, 0.6),
                    hash(vec2(fi, 5.0))
                );
                bg += pColor * particleAlpha * 0.4;
            }

            bg = max(bg, 0.0);
            bg = pow(bg, vec3(0.92));
            gl_FragColor = vec4(bg, 1.0);
        }
    `;

    function createShader(type, source) {
        const shader = gl.createShader(type);
        gl.shaderSource(shader, source);
        gl.compileShader(shader);
        if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
            console.error('Shader error:', gl.getShaderInfoLog(shader));
            gl.deleteShader(shader);
            return null;
        }
        return shader;
    }

    const vs = createShader(gl.VERTEX_SHADER, vsSource);
    const fs = createShader(gl.FRAGMENT_SHADER, fsSource);

    if (vs && fs) {
        const program = gl.createProgram();
        gl.attachShader(program, vs);
        gl.attachShader(program, fs);
        gl.linkProgram(program);

        if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
            console.error('Program error:', gl.getProgramInfoLog(program));
        } else {
            gl.useProgram(program);

            const buffer = gl.createBuffer();
            gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
            gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW);

            const aPos = gl.getAttribLocation(program, 'aPos');
            gl.enableVertexAttribArray(aPos);
            gl.vertexAttribPointer(aPos, 2, gl.FLOAT, false, 0, 0);

            const uResolution = gl.getUniformLocation(program, 'iResolution');
            const uTime = gl.getUniformLocation(program, 'iTime');

            resizeCanvas();
            window.addEventListener('resize', resizeCanvas);

            const start = performance.now();

            function render() {
                const t = (performance.now() - start) / 1000;
                gl.uniform2f(uResolution, canvas.width, canvas.height);
                gl.uniform1f(uTime, t);
                gl.drawArrays(gl.TRIANGLES, 0, 3);
                animationId = requestAnimationFrame(render);
            }
            render();
        }
    }
}

// ==================== 加载提示文字轮换 ====================
const loadingText = document.getElementById('loading-text');
const loadingBar = document.getElementById('loading-bar');
const enterBtnWrap = document.getElementById('enter-btn-wrap');
const enterBtn = document.getElementById('enter-btn');

const loadingMessages = [
    'Initializing',
    'Loading engines',
    'Connecting modules',
    'Preparing workspace',
    'Almost ready',
];

let msgIndex = 0;
const msgInterval = setInterval(() => {
    msgIndex = (msgIndex + 1) % loadingMessages.length;
    loadingText.textContent = loadingMessages[msgIndex];
}, 3000);

// ==================== 打字机效果（Claude CLI 风格，参考 Vibe Motion claude-typer） ====================
function typewriter(element, text, baseSpeed, callback) {
    element.classList.add('typing');
    element.innerHTML = '';
    let i = 0;
    const cursor = document.createElement('span');
    cursor.className = 'title-cursor';
    element.appendChild(cursor);

    // Claude CLI 风格：变频输入（空格/标点更快，字母略慢）
    function getDelay(char) {
        if (char === ' ') return baseSpeed * 0.3;
        if (/[.,;:!?]/.test(char)) return baseSpeed * 1.5;
        if (/[A-Z]/.test(char)) return baseSpeed * 1.2;
        return baseSpeed * (0.7 + Math.random() * 0.6); // 随机抖动
    }

    function type() {
        if (i < text.length) {
            const char = text.charAt(i);
            const span = document.createElement('span');
            span.className = 'char';
            span.textContent = char;
            // 空格用 wider span 代替，避免 inline-block 吞掉空格宽度
            if (char === ' ') {
                span.style.width = '14px';
                span.style.display = 'inline-block';
            }
            cursor.before(span);

            // 字符出现时短暂发光（Claude CLI 风格）
            if (char !== ' ') {
                span.classList.add('glow');
                setTimeout(() => span.classList.remove('glow'), 200);
            }

            i++;
            setTimeout(type, getDelay(char));
        } else {
            // 打完后延迟隐藏光标
            setTimeout(() => {
                cursor.classList.add('hidden');
                if (callback) callback();
            }, 800);
        }
    }
    type();
}

// 启动打字机动画（徽记组装完成后开始）
setTimeout(() => {
    const titleEl = document.getElementById('title-text');
    const subtitleEl = document.getElementById('subtitle-text');

    typewriter(titleEl, 'Xiaoda Agent', 95, () => {
        typewriter(subtitleEl, 'AI Multi-Agent Assistant', 45, null);
    });
}, 2000);

// ==================== Enter 按钮 3D 倾斜效果 ====================
(function initButton3D() {
    const wrap = enterBtnWrap;
    const btn = enterBtn;
    const maxTilt = 15; // 最大倾斜角度

    wrap.addEventListener('mousemove', (e) => {
        const rect = btn.getBoundingClientRect();
        const x = e.clientX - rect.left;
        const y = e.clientY - rect.top;
        const cx = rect.width / 2;
        const cy = rect.height / 2;

        // 计算倾斜（鼠标在上方 → 按钮向前倾）
        const rotateY = ((x - cx) / cx) * maxTilt;
        const rotateX = ((cy - y) / cy) * maxTilt;

        btn.style.transform = `rotateX(${rotateX}deg) rotateY(${rotateY}deg) translateZ(8px)`;

        // 反光层跟随鼠标
        const mx = ((x / rect.width) * 100).toFixed(1);
        const my = ((y / rect.height) * 100).toFixed(1);
        btn.style.setProperty('--mx', mx + '%');
        btn.style.setProperty('--my', my + '%');
    });

    wrap.addEventListener('mouseleave', () => {
        btn.style.transform = 'rotateX(0deg) rotateY(0deg) translateZ(0px)';
    });
})();

// ==================== 服务就绪回调（由 Python 端调用） ====================

window.onServerReady = function() {
    clearInterval(msgInterval);
    loadingText.textContent = 'Ready';
    if (loadingBar) loadingBar.style.animation = 'none';
    enterBtnWrap.classList.add('ready');
};

window.onServerTimeout = function() {
    clearInterval(msgInterval);
    loadingText.textContent = 'Connection timeout';
};

// ==================== 墨迹转场 + 进入按钮点击 ====================

/**
 * 墨迹擦除转场（参考 InkReveal 算法，纳西妲草绿色风格）
 * 点击 Enter 后，从按钮位置开始扩散草绿墨迹，覆盖全屏后跳转 WebUI
 */
function inkTransition(originX, originY, onComplete) {
    const canvas = document.getElementById('ink-overlay');
    if (!canvas) { onComplete(); return; }

    const ctx = canvas.getContext('2d');
    const W = window.innerWidth;
    const H = window.innerHeight;
    canvas.width = W;
    canvas.height = H;
    canvas.classList.add('active');

    // 墨迹印记：从原点 + 周围散布多个点，模拟墨水晕染
    const diag = Math.hypot(W, H);
    const stamps = [];
    const now = performance.now() / 1000;

    // 主墨迹点（从按钮位置）
    stamps.push({ x: originX, y: originY, rmax: diag * 0.7, born: now, delay: 0 });

    // 辅助墨迹点（围绕原点散开，制造不规则边缘）
    const auxCount = 8;
    for (let i = 0; i < auxCount; i++) {
        const angle = (i / auxCount) * Math.PI * 2 + Math.random() * 0.5;
        const dist = 60 + Math.random() * 120;
        stamps.push({
            x: originX + Math.cos(angle) * dist,
            y: originY + Math.sin(angle) * dist,
            rmax: diag * (0.35 + Math.random() * 0.3),
            born: now,
            delay: 0.1 + Math.random() * 0.3,
        });
    }

    // 远端墨迹点（从屏幕角落向中心扩散，加速覆盖）
    const corners = [
        { x: 0, y: 0 }, { x: W, y: 0 },
        { x: 0, y: H }, { x: W, y: H },
        { x: W * 0.5, y: 0 }, { x: W * 0.5, y: H },
        { x: 0, y: H * 0.5 }, { x: W, y: H * 0.5 },
    ];
    corners.forEach((c, i) => {
        stamps.push({
            x: c.x, y: c.y,
            rmax: diag * (0.4 + Math.random() * 0.25),
            born: now,
            delay: 0.25 + i * 0.05,
        });
    });

    const lifetime = 1.0; // 每个墨迹的生命周期（秒）
    const rStart = 3;
    const totalDuration = lifetime + 0.5; // 总转场时间
    const startTime = performance.now();

    // 墨迹颜色：深草绿底色
    const inkColor = '#0f1f17';
    const inkGlow = 'rgba(127, 214, 80, 0.15)';

    function loop() {
        const elapsed = (performance.now() - startTime) / 1000;
        ctx.clearRect(0, 0, W, H);

        let allDone = true;
        for (const s of stamps) {
            const t = elapsed - s.delay;
            if (t < 0) { allDone = false; continue; }
            const lt = t / lifetime;
            if (lt >= 1) continue;
            allDone = false;

            // InkReveal 缓出曲线：ease = 1 - (1-t)³
            const ease = 1 - Math.pow(1 - lt, 3);
            const r = rStart + (s.rmax - rStart) * ease;
            // 转场用：alpha 从 0 → 1（覆盖屏幕）
            const alpha = Math.min(lt * 1.5, 1);

            // 主墨迹圆
            ctx.save();
            ctx.globalAlpha = alpha;
            ctx.fillStyle = inkColor;
            ctx.beginPath();
            ctx.arc(s.x, s.y, Math.max(r, 1), 0, Math.PI * 2);
            ctx.fill();

            // 草绿色光晕边缘（纳西妲风格）
            if (lt < 0.7) {
                ctx.globalAlpha = alpha * 0.3 * (1 - lt / 0.7);
                ctx.fillStyle = inkGlow;
                ctx.beginPath();
                ctx.arc(s.x, s.y, Math.max(r * 1.15, 1), 0, Math.PI * 2);
                ctx.fill();
            }
            ctx.restore();
        }

        if (!allDone) {
            requestAnimationFrame(loop);
        } else {
            // 完全覆盖后，稍等一瞬再跳转
            setTimeout(onComplete, 100);
        }
    }
    requestAnimationFrame(loop);
}

enterBtn.addEventListener('click', () => {
    enterBtn.style.opacity = '0.5';
    enterBtn.style.pointerEvents = 'none';

    // 获取按钮中心位置作为墨迹原点
    const rect = enterBtn.getBoundingClientRect();
    const cx = rect.left + rect.width / 2;
    const cy = rect.top + rect.height / 2;

    const port = window.location.hash ? window.location.hash.substring(1) : '8082';
    inkTransition(cx, cy, () => {
        window.location.href = `http://localhost:${port}`;
    });
});

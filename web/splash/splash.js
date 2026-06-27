// ==================== 全屏 WebGL Shader（渐变背景 + 粒子 + 波浪 + 极光） ====================
// 参考 Obsidian MCP: SiriWave wave 变体 + GlowHorizon 光晕 + 宇宙星图粒子
const canvas = document.getElementById('bg-canvas');
const gl = canvas.getContext('webgl', { alpha: false, premultipliedAlpha: false })
    || canvas.getContext('experimental-webgl', { alpha: false, premultipliedAlpha: false });

let animationId = null;

function resizeCanvas() {
    const w = window.innerWidth;
    const h = window.innerHeight;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
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

    // 全屏 fragment shader：渐变背景 + 极光底晕 + 波浪线 + 粒子
    const fsSource = `
        precision highp float;
        uniform vec2 iResolution;
        uniform float iTime;

        // 伪随机（纹理坐标哈希）
        float hash(vec2 p) {
            return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453);
        }

        // 2D 噪声
        float noise(vec2 p) {
            vec2 i = floor(p);
            vec2 f = fract(p);
            float a = hash(i);
            float b = hash(i + vec2(1.0, 0.0));
            float c = hash(i + vec2(0.0, 1.0));
            float d = hash(i + vec2(1.0, 1.0));
            vec2 u = f * f * (3.0 - 2.0 * f);
            return mix(a, b, u.x) + (c - a) * u.y * (1.0 - u.x) + (d - b) * u.x * u.y;
        }

        void main() {
            vec2 uv = gl_FragCoord.xy / iResolution;
            float t = iTime;
            float aspect = iResolution.x / iResolution.y;

            // === 1. 渐变背景（从深森林到较亮的绿色） ===
            // 垂直渐变：底部亮 → 顶部暗
            vec3 bgTop = vec3(0.051, 0.078, 0.059);      // #0d140f
            vec3 bgBot = vec3(0.098, 0.165, 0.122);      // #192a1f
            vec3 bg = mix(bgBot, bgTop, uv.y);

            // 添加微妙的径向光照（从中心偏下向上散开）
            vec2 center = vec2(0.5, 0.35);
            float radial = length((uv - center) * vec2(aspect, 1.0));
            bg += vec3(0.02, 0.04, 0.025) * smoothstep(0.8, 0.0, radial);

            // === 2. 极光底晕（参考 GlowHorizon 分层光晕，4层） ===
            // 底部第1层：深绿色底层（最宽最模糊）
            float a1 = smoothstep(0.0, 0.5, uv.y);
            a1 = 1.0 - a1;
            a1 = a1 * a1;
            bg += vec3(0.02, 0.06, 0.03) * a1 * 0.8;

            // 底部第2层：草绿色主光晕
            float a2 = smoothstep(0.0, 0.35, uv.y);
            a2 = 1.0 - a2;
            a2 = a2 * a2 * a2;
            bg += vec3(0.06, 0.14, 0.07) * a2 * 1.2;

            // 底部第3层：亮绿色高光（窄而亮）
            float a3 = smoothstep(0.0, 0.18, uv.y);
            a3 = 1.0 - a3;
            a3 = a3 * a3 * a3 * a3;
            bg += vec3(0.08, 0.18, 0.06) * a3 * 1.5;

            // 底部第4层：白色高光（最窄最亮，GlowHorizon 白色高光层）
            float a4 = smoothstep(0.0, 0.08, uv.y);
            a4 = 1.0 - a4;
            a4 = a4 * a4 * a4 * a4 * a4;
            bg += vec3(0.12, 0.18, 0.10) * a4 * 0.6;

            // 极光脉动（让光晕呼吸）
            float pulse = sin(t * 0.5) * 0.5 + 0.5;
            bg += vec3(0.01, 0.02, 0.01) * a2 * pulse;

            // === 3. 波浪线（参考 SiriWave wave 变体，从底部上涌） ===
            // 多层波形叠加（主波 + 副波 + 细节波 + 噪声扭曲）
            float wx = uv.x * aspect; // 修正宽高比
            float wave1 = sin(wx * 12.0 + t * 1.5) * 0.06;
            float wave2 = sin(wx * 20.0 - t * 2.0) * 0.035;
            float wave3 = sin(wx * 35.0 + t * 3.0) * 0.018;
            float waveNoise = (noise(vec2(wx * 4.0 + t * 0.5, t * 0.3)) - 0.5) * 0.025;
            float waveY = 0.15 + wave1 + wave2 + wave3 + waveNoise;

            // 波浪发光（距离函数，越近越亮）
            float dw = uv.y - waveY;
            float waveGlow = 0.008 / (abs(dw) + 0.003);
            waveGlow = clamp(waveGlow, 0.0, 8.0);

            // 波浪下方区域填充（半透明绿色雾）
            float waveFill = smoothstep(waveY - 0.05, waveY, uv.y);
            waveFill = 1.0 - waveFill;
            waveFill *= waveFill * 0.15;
            bg += vec3(0.20, 0.50, 0.15) * waveFill;

            // 波浪线发光
            bg += vec3(0.25, 0.55, 0.15) * waveGlow * 0.3;

            // 第二条波浪（更高、更淡、更慢）
            float wave2Y = 0.28 + sin(wx * 8.0 - t * 1.0) * 0.04
                         + sin(wx * 18.0 + t * 1.5) * 0.02;
            float dw2 = uv.y - wave2Y;
            float waveGlow2 = 0.005 / (abs(dw2) + 0.005);
            waveGlow2 = clamp(waveGlow2, 0.0, 5.0);
            bg += vec3(0.15, 0.35, 0.10) * waveGlow2 * 0.15;

            // === 4. 浮动粒子（参考宇宙星图粒子系统） ===
            for (int i = 0; i < 80; i++) {
                float fi = float(i);
                // 粒子初始位置（伪随机分布）
                float px = hash(vec2(fi * 0.17, 0.31));
                float py = hash(vec2(fi * 0.23, 0.67));

                // 粒子漂浮运动（每个粒子独立速度）
                float speed = 0.02 + hash(vec2(fi, 0.0)) * 0.04;
                float drift = sin(t * speed + fi) * 0.08;
                float rise = t * (0.003 + hash(vec2(fi, 1.0)) * 0.005);

                // 循环：粒子上升后从底部重新出现
                float fy = mod(py + rise, 1.2) - 0.1;
                float fx = mod(px + drift + sin(t * 0.3 + fi * 0.5) * 0.03, 1.0);

                // 粒子大小和亮度
                float size = 0.001 + hash(vec2(fi, 2.0)) * 0.003;
                float brightness = 0.3 + hash(vec2(fi, 3.0)) * 0.5;

                // 闪烁
                float twinkle = sin(t * (2.0 + hash(vec2(fi, 4.0)) * 3.0) + fi * 6.28) * 0.3 + 0.7;

                // 距离衰减
                vec2 pPos = vec2(fx, fy);
                float dist = length((uv - pPos) * vec2(aspect, 1.0));
                float particleAlpha = smoothstep(size * 2.0, 0.0, dist) * brightness * twinkle;

                // 粒子颜色：草绿色为主，部分偏亮白
                vec3 pColor = mix(
                    vec3(0.35, 0.75, 0.25),  // 草绿
                    vec3(0.7, 0.95, 0.6),     // 亮绿白
                    hash(vec2(fi, 5.0))
                );
                bg += pColor * particleAlpha * 0.4;
            }

            // === 5. 柔和噪点纹理（增加质感，防止色带） ===
            float grain = (hash(uv * iResolution.xy + t * 100.0) - 0.5) * 0.015;
            bg += grain;

            // 色调映射 + gamma
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

    typewriter(titleEl, 'Nahida Agent', 95, () => {
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

// ==================== 进入按钮点击事件 ====================
enterBtn.addEventListener('click', async () => {
    enterBtn.style.opacity = '0.5';
    enterBtn.style.pointerEvents = 'none';
    try {
        await window.pywebview.api.enter_world();
    } catch (e) {
        const port = window.location.hash ? window.location.hash.substring(1) : '8082';
        window.location.href = `http://localhost:${port}`;
    }
});

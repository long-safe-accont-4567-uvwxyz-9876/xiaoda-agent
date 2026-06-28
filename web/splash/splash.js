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

// ==================== 服务就绪回调（Python 端 evaluate_js 调用） ====================

var _serverReadyCalled = false;

window.onServerReady = function() {
    if (_serverReadyCalled) return;
    _serverReadyCalled = true;
    clearInterval(msgInterval);
    loadingText.textContent = 'Ready';
    if (loadingBar) loadingBar.style.animation = 'none';
    enterBtnWrap.classList.add('ready');
    // 预加载 WebUI 到 iframe（display:block + opacity:0 在 ink-canvas 下面预加载）
    var frame = document.getElementById('webui-frame');
    if (frame) {
        var port = window.__SPLASH_PORT || (location.hash.length > 1 ? location.hash.substring(1) : '8082');
        frame.src = 'http://localhost:' + port;
        frame.style.display = 'block';
        frame.style.opacity = '0';
    }
};

window.onServerTimeout = function() {
    clearInterval(msgInterval);
    loadingText.textContent = 'Connection timeout';
};

// 预览模式：自动显示 Enter 按钮（仅调试用）
if (new URLSearchParams(window.location.search).has('preview')) {
    setTimeout(() => window.onServerReady(), 2500);
}

// ==================== InkReveal 墨迹转场 ====================
/**
 * 按 Obsidian InkReveal 灵动设计：
 *   多层波浪扩散 + 不规则墨点 + 飞溅簇 + 变速扩散 + 有机路径
 */
// InkReveal 完成后通过 pywebview js_api 将窗口导航到 WebUI（无需 iframe）
// 读取端口：URL hash（file:// 模式）或默认 8082
(function() {
    window.__SPLASH_PORT = (location.hash.length > 1 ? location.hash.substring(1) : '8082');
})();

function inkRevealTransition(originX, originY, onComplete) {
    var inkCanvas = document.getElementById('ink-canvas');
    var webuiFrame = document.getElementById('webui-frame');
    if (!inkCanvas) { if (onComplete) onComplete(); return; }

    // 确保 iframe 可见（在 ink-canvas 下面，墨水擦除后透出 WebUI）
    if (webuiFrame) {
        webuiFrame.style.opacity = '1';
    }

    var dpr = Math.min(devicePixelRatio, 1.5);
    var W = innerWidth, H = innerHeight;
    inkCanvas.width = W * dpr;
    inkCanvas.height = H * dpr;
    inkCanvas.style.width = W + 'px';
    inkCanvas.style.height = H + 'px';
    var ctx = inkCanvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

    // 截取当前 splash 画面到 ink-canvas
    var bgCanvas = document.getElementById('bg-canvas');

    // 先画背景色
    var g = ctx.createLinearGradient(0, 0, 0, H);
    g.addColorStop(0, '#0d140f'); g.addColorStop(0.5, '#162a1c'); g.addColorStop(1, '#192a1f');
    ctx.fillStyle = g; ctx.fillRect(0, 0, W, H);

    // 截取 WebGL canvas
    if (bgCanvas) {
        try { ctx.drawImage(bgCanvas, 0, 0, W, H); } catch(e) {}
    }

    inkCanvas.style.display = 'block';

    // 隐藏所有 splash 元素（iframe 在下面已经可见）
    if (bgCanvas) bgCanvas.style.display = 'none';
    var vineFrame = document.querySelector('.vine-frame');
    if (vineFrame) vineFrame.style.display = 'none';
    var center = document.querySelector('.center-content');
    if (center) center.style.display = 'none';

    var diag = Math.sqrt(W * W + H * H);
    var now = performance.now() / 1000;
    var stamps = [];

    // 种子函数
    function rand(n) { return Math.random() * n; }
    function rrange(a, b) { return a + rand(b - a); }

    // ===== 波浪1：核心爆裂（按钮周围，快出快散） =====
     for (var i = 0; i < 8; i++) {
         var angle = rand(6.283);
         var dist = rand(180);
         stamps.push({
             x: originX + Math.cos(angle) * dist, y: originY + Math.sin(angle) * dist,
             born: now + rand(0.1),
             rmax: rrange(diag * 0.2, diag * 0.45),
             life: rrange(0.5, 0.7)
         });
         // 飞溅小点
         if (rand(1) < 0.5) {
             stamps.push({
                 x: originX + Math.cos(angle) * (dist + rand(50)), y: originY + Math.sin(angle) * (dist + rand(50)),
                 born: now + 0.04 + rand(0.06),
                 rmax: rrange(diag * 0.1, diag * 0.22),
                 life: rrange(0.35, 0.5)
             });
         }
     }

     // ===== 波浪2：有机扩散（沿扇形方向，层次推进） =====
     var branches = 5;
     for (var b = 0; b < branches; b++) {
         var ba = (b / branches) * 6.283 + rand(0.3);
         var bx = originX, by = originY;
         var steps = 4 + Math.floor(rand(3));
         for (var s = 0; s < steps; s++) {
             ba += rrange(-0.35, 0.35);
             var sd = 70 + s * rrange(100, 220);
             bx = Math.min(W, Math.max(0, bx + Math.cos(ba) * sd));
             by = Math.min(H, Math.max(0, by + Math.sin(ba) * sd));
             stamps.push({
                 x: bx + rrange(-35, 35), y: by + rrange(-35, 35),
                 born: now + 0.08 + (b * 0.06) + (s * 0.07),
                 rmax: rrange(diag * 0.25, diag * 0.55),
                 life: rrange(0.6, 0.85)
             });
         }
     }

     // ===== 波浪3：收边（四角 & 边缘，最后一批） =====
     var edges = [[0,0],[W,0],[0,H],[W,H],[W*.5,0],[W*.5,H],[0,H*.5],[W,H*.5],[rand(W),rand(H)],[rand(W),rand(H)]];
     for (var e = 0; e < edges.length; e++) {
         stamps.push({
             x: edges[e][0], y: edges[e][1],
             born: now + 0.3 + rand(0.35),
             rmax: rrange(diag * 0.3, diag * 0.6),
             life: rrange(0.6, 0.8)
         });
     }

     // 飞溅微点（散布全屏）
     for (var sp = 0; sp < 12; sp++) {
         stamps.push({
             x: rand(W), y: rand(H),
             born: now + 0.15 + rand(0.4),
             rmax: rrange(diag * 0.06, diag * 0.2),
             life: rrange(0.3, 0.5)
         });
     }

    var rStart = 1.5;
    var finished = false;

    function loop() {
        var t = performance.now() / 1000;
        var alive = false;

        for (var i = stamps.length - 1; i >= 0; i--) {
            var s = stamps[i];
            var p = (t - s.born) / s.life;
            if (p < 0) { alive = true; continue; }
            if (p >= 1) { stamps.splice(i, 1); continue; }
            alive = true;

            // 缓出曲线
             var ease = 1 - Math.pow(1 - p, 2.8);
             var r = rStart + (s.rmax - rStart) * ease;

             // 草绿光晕
             ctx.save();
             ctx.globalCompositeOperation = 'source-over';
             var gw = Math.max(2, r * 0.25);
             var gg = ctx.createRadialGradient(s.x, s.y, Math.max(0, r - gw), s.x, s.y, r);
             gg.addColorStop(0, 'rgba(143,229,96,0)');
             gg.addColorStop(0.5, 'rgba(143,229,96,0.1)');
             gg.addColorStop(1, 'rgba(143,229,96,0)');
             ctx.fillStyle = gg;
             ctx.beginPath(); ctx.arc(s.x, s.y, r, 0, 6.283); ctx.fill();
             ctx.restore();

             // 挖洞
             ctx.save();
             ctx.globalCompositeOperation = 'destination-out';
             var ir = Math.max(0, r * 0.75);
             var cg = ctx.createRadialGradient(s.x, s.y, ir, s.x, s.y, r);
             cg.addColorStop(0, 'rgba(0,0,0,1)');
             cg.addColorStop(0.8, 'rgba(0,0,0,0.5)');
             cg.addColorStop(1, 'rgba(0,0,0,0)');
             ctx.fillStyle = cg;
             ctx.beginPath(); ctx.arc(s.x, s.y, r, 0, 6.283); ctx.fill();
             ctx.restore();
        }

        if (!alive && !finished) {
            finished = true;
            ctx.save();
            ctx.globalCompositeOperation = 'destination-out';
            ctx.fillStyle = 'rgba(0,0,0,1)';
            ctx.fillRect(0, 0, W, H);
            ctx.restore();
            setTimeout(function() {
                canvas.style.display = 'none';
                ctx.clearRect(0, 0, W, H);
                if (onComplete) onComplete();
            }, 400);
        } else if (alive) {
            requestAnimationFrame(loop);
        }
    }
    requestAnimationFrame(loop);
}

enterBtn.addEventListener('click', function() {
    enterBtn.style.opacity = '0.5';
    enterBtn.style.pointerEvents = 'none';

    var rect = enterBtn.getBoundingClientRect();
    var cx = rect.left + rect.width / 2;
    var cy = rect.top + rect.height / 2;

    inkRevealTransition(cx, cy, function() {
        // 转场完成，移除 ink-canvas，Web UI 已经通过 iframe 显示
        var inkCanvas = document.getElementById('ink-canvas');
        if (inkCanvas) inkCanvas.style.display = 'none';
    });
});

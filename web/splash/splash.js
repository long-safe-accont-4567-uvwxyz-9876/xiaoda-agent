// ==================== SiriWave WebGL 水平波浪（草绿色系 wave 变体） ====================
const canvas = document.getElementById('siri-wave');
const gl = canvas.getContext('webgl', { alpha: true, premultipliedAlpha: true })
    || canvas.getContext('experimental-webgl', { alpha: true, premultipliedAlpha: true });

let animationId = null;

function resizeCanvas() {
    const w = window.innerWidth;
    const h = 100;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
    canvas.style.width = w + 'px';
    canvas.style.height = h + 'px';
    if (gl) gl.viewport(0, 0, canvas.width, canvas.height);
}

if (gl) {
    gl.enable(gl.BLEND);
    gl.blendFunc(gl.ONE, gl.ONE_MINUS_SRC_ALPHA);

    const vsSource = `
        attribute vec2 aPos;
        void main() {
            gl_Position = vec4(aPos, 0.0, 1.0);
        }
    `;

    // SiriWave wave 变体 — 水平波浪线条，草绿色
    // 参考 Obsidian SiriWave 设计提示词，3层正弦波叠加 + 发光效果
    const fsSource = `
        precision highp float;
        uniform vec2 iResolution;
        uniform float iTime;

        void main() {
            vec2 uv = gl_FragCoord.xy / iResolution;
            float t = iTime;

            // 3层波形叠加（主波 + 副波 + 细节波）
            float wave = sin(uv.x * 20.0 + t * 3.0) * 0.12;
            wave += sin(uv.x * 35.0 - t * 2.5) * 0.06;
            wave += sin(uv.x * 50.0 + t * 4.0) * 0.03;

            // 中心波形距离
            float d = uv.y - (0.5 + wave);
            float glow = 0.015 / abs(d);

            // 草绿色 #8fe560 = vec3(0.56, 0.90, 0.38)
            vec3 dendroColor = vec3(0.56, 0.90, 0.38);
            vec3 color = dendroColor * glow;

            // 边缘渐隐（左右渐出，上下渐出）
            float edgeX = smoothstep(0.0, 0.08, uv.x) * smoothstep(1.0, 0.92, uv.x);
            float edgeY = smoothstep(0.0, 0.15, uv.y) * smoothstep(1.0, 0.85, uv.y);
            color *= edgeX * edgeY;

            // 预乘 alpha
            float brightness = dot(color, vec3(0.299, 0.587, 0.114));
            float alpha = clamp(brightness * 1.5, 0.0, 0.85);
            gl_FragColor = vec4(color * alpha, alpha);
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

// ==================== 服务就绪回调（由 Python 端调用） ====================

window.onServerReady = function() {
    clearInterval(msgInterval);
    loadingText.textContent = 'Ready';
    if (loadingBar) loadingBar.style.animation = 'none';
    enterBtn.classList.add('ready');
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

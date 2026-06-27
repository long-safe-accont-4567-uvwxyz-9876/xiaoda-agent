// ==================== SiriWave WebGL 圆形波纹（纳西妲草绿色系） ====================
const canvas = document.getElementById('siri-wave');
const gl = canvas.getContext('webgl') || canvas.getContext('experimental-webgl');

let animationId = null;

function resizeCanvas() {
    const size = Math.min(window.innerWidth, window.innerHeight) * 0.6;
    const renderScale = 1.5;
    const dim = Math.round(size * renderScale);
    canvas.width = dim;
    canvas.height = dim;
    canvas.style.width = size + 'px';
    canvas.style.height = size + 'px';
    if (gl) gl.viewport(0, 0, dim, dim);
}

if (gl) {
    const vsSource = `
        attribute vec2 aPos;
        void main() {
            gl_Position = vec4(aPos, 0.0, 1.0);
        }
    `;

    // 圆形波纹 Shader - 从中心向外扩散的多层环形波纹
    // 颜色：草元素绿 #8fe560 = rgb(143, 229, 96) = vec3(0.56, 0.90, 0.38)
    const fsSource = `
        precision highp float;
        uniform vec2 iResolution;
        uniform float iTime;

        void main() {
            vec2 uv = gl_FragCoord.xy / iResolution.xy;
            vec2 center = vec2(0.5, 0.5);
            float dist = distance(uv, center);

            // 多层环形波纹 - 从中心向外扩散
            float wave1 = sin(dist * 30.0 - iTime * 2.0);
            float wave2 = sin(dist * 50.0 - iTime * 3.5);
            float wave3 = sin(dist * 70.0 - iTime * 5.0);

            float waves = wave1 * 0.5 + wave2 * 0.3 + wave3 * 0.2;

            // 距离衰减 - 中心亮，边缘暗
            float falloff = 1.0 - smoothstep(0.0, 0.5, dist);
            falloff = falloff * falloff;

            // 发光环带
            float ring = 0.04 / abs(waves);

            // 草元素绿色
            vec3 dendroColor = vec3(0.56, 0.90, 0.38);
            vec3 color = dendroColor * ring * falloff;

            // 中心发光
            float centerGlow = 0.08 / (dist + 0.08);
            color += dendroColor * centerGlow * 0.2;

            gl_FragColor = vec4(color, 1.0);
        }
    `;

    function createShader(type, source) {
        const shader = gl.createShader(type);
        gl.shaderSource(shader, source);
        gl.compileShader(shader);
        return shader;
    }

    const vs = createShader(gl.VERTEX_SHADER, vsSource);
    const fs = createShader(gl.FRAGMENT_SHADER, fsSource);
    const program = gl.createProgram();
    gl.attachShader(program, vs);
    gl.attachShader(program, fs);
    gl.linkProgram(program);
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
} else {
    console.warn('WebGL not supported, splash animation disabled');
}

// ==================== 加载提示文字轮换 ====================
const loadingText = document.getElementById('loading-text');
const enterBtn = document.getElementById('enter-btn');

const loadingMessages = [
    '世界树正在苏醒...',
    '梦境之花绽放中...',
    '草元素之力凝聚...',
    '地脉能量连接中...',
    '知识之叶飘落...',
];

let msgIndex = 0;
const msgInterval = setInterval(() => {
    msgIndex = (msgIndex + 1) % loadingMessages.length;
    loadingText.textContent = loadingMessages[msgIndex];
}, 2500);

// ==================== 服务就绪回调（由 Python 端调用） ====================

// Python 端服务就绪后调用此函数
window.onServerReady = function() {
    clearInterval(msgInterval);
    loadingText.textContent = '世界树已苏醒';
    enterBtn.classList.add('ready');
};

// Python 端超时后调用此函数
window.onServerTimeout = function() {
    clearInterval(msgInterval);
    loadingText.textContent = '连接超时，请检查服务状态';
};

// ==================== 进入按钮点击事件 ====================
enterBtn.addEventListener('click', async () => {
    enterBtn.style.opacity = '0.5';
    enterBtn.style.pointerEvents = 'none';
    try {
        // 通过 pywebview JS API 调用 Python 端切换 URL
        await window.pywebview.api.enter_world();
    } catch (e) {
        // Fallback: 直接跳转
        const port = window.location.hash ? window.location.hash.substring(1) : '8082';
        window.location.href = `http://localhost:${port}`;
    }
});

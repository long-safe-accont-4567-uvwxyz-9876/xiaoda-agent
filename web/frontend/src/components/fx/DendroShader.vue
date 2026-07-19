<script setup lang="ts">
import { onMounted, onBeforeUnmount, ref } from 'vue'
import * as THREE from 'three'

const containerRef = ref<HTMLDivElement | null>(null)

let renderer: THREE.WebGLRenderer | null = null
let scene: THREE.Scene | null = null
let camera: THREE.OrthographicCamera | null = null
let mesh: THREE.Mesh | null = null
let raf = 0
let clock: THREE.Clock | null = null

const vertexShader = `
void main() {
  gl_Position = vec4(position, 1.0);
}
`

const fragmentShader = `
precision highp float;
uniform vec2 resolution;
uniform float time;

// 须弥草元素配色 v2
vec3 dendro = vec3(0.498, 0.839, 0.314);   // #7fd650 草光
vec3 jade   = vec3(0.310, 0.839, 0.647);   // #4fd6a5 翡翠
vec3 sprout = vec3(0.722, 1.000, 0.522);   // #b8ff85 嫩芽
vec3 forest = vec3(0.059, 0.122, 0.090);   // #0f1f17 深林
vec3 wisdom = vec3(0.910, 0.835, 0.639);   // #e8d5a3 曦金

float random(vec2 st) {
    return fract(sin(dot(st.xy, vec2(12.9898,78.233))) * 43758.5453123);
}

float noise(vec2 st) {
    vec2 i = floor(st);
    vec2 f = fract(st);
    float a = random(i);
    float b = random(i + vec2(1.0, 0.0));
    float c = random(i + vec2(0.0, 1.0));
    float d = random(i + vec2(1.0, 1.0));
    vec2 u = f * f * (3.0 - 2.0 * f);
    return mix(a, b, u.x) + (c - a) * u.y * (1.0 - u.x) + (d - b) * u.x * u.y;
}

float fbm(vec2 st) {
    float v = 0.0;
    float amp = 0.55;
    for (int i = 0; i < 4; i++) {
        v += amp * noise(st);
        st = st * 2.03 + vec2(11.3, 7.7);
        amp *= 0.5;
    }
    return v;
}

// 单只萤火虫：网格哈希定位 + 慢漂移 + 闪烁
float firefly(vec2 uv, vec2 cell, float t) {
    vec2 base = vec2(random(cell), random(cell + 19.19));
    vec2 pos = base + 0.28 * vec2(sin(t * 0.31 + base.x * 6.28), cos(t * 0.23 + base.y * 6.28));
    float d = length(uv - pos);
    float tw = 0.5 + 0.5 * sin(t * (1.2 + base.y) + base.x * 40.0);
    return smoothstep(0.028, 0.0, d) * tw * tw;
}

void main(void) {
    vec2 uv = (gl_FragCoord.xy * 2.0 - resolution.xy) / min(resolution.x, resolution.y);
    float t = time * 0.03;

    // 深林基底：底部略亮，像林下透光
    vec3 color = forest * (0.85 + 0.3 * (1.0 - abs(uv.y)));

    // 极光丝带：域扭曲 fbm，三条交错的草元素光带
    vec2 warp = vec2(fbm(uv * 1.4 + t * 0.4), fbm(uv * 1.4 - t * 0.3 + 5.2));
    vec2 suv = uv + 0.35 * warp;
    float band1 = smoothstep(0.24, 0.0, abs(suv.y - 0.45 * sin(suv.x * 1.2 + t * 1.6)));
    float band2 = smoothstep(0.20, 0.0, abs(suv.y + 0.5 * sin(suv.x * 0.9 - t * 1.2 + 2.0) + 0.35));
    float band3 = smoothstep(0.16, 0.0, abs(suv.y - 0.3 * sin(suv.x * 1.7 + t * 2.0 + 4.0) - 0.5));
    color += dendro * band1 * 0.20;
    color += jade   * band2 * 0.16;
    color += sprout * band3 * 0.10;

    // 草叶流动（原有多层噪声，降幅作为底纹）
    for(int i = 0; i < 4; i++) {
        float fi = float(i);
        vec2 flow = vec2(
            noise(uv * (2.0 + fi * 0.5) + vec2(t * 0.3, t * 0.1 * fi)),
            noise(uv * (2.0 + fi * 0.5) + vec2(t * 0.1 * fi, t * 0.2))
        );
        float leaf = smoothstep(0.4, 0.6, flow.x * flow.y);
        color = mix(color, dendro * (0.3 + 0.15 * fi), leaf * 0.16);
    }

    // 萤火光斑：3x3 邻域累积，玉青与曦金交错
    vec2 fuv = uv * 2.6;
    vec2 cellId = floor(fuv);
    float tf = time * 0.6;
    float glowSum = 0.0;
    float goldSum = 0.0;
    for (int gx = -1; gx <= 1; gx++) {
        for (int gy = -1; gy <= 1; gy++) {
            vec2 c = cellId + vec2(float(gx), float(gy));
            float f = firefly(fract(fuv) - vec2(float(gx), float(gy)) + vec2(0.0), c, tf);
            if (random(c + 7.7) > 0.72) goldSum += f; else glowSum += f;
        }
    }
    color += jade   * glowSum * 0.35;
    color += wisdom * goldSum * 0.40;

    // 露珠闪烁：高频微光
    float sparkle = noise(uv * 9.0 + t * 0.55);
    sparkle = pow(sparkle, 9.0);
    color += wisdom * sparkle * 0.18;

    // 顶部月光 + 边缘暗角
    float moon = smoothstep(1.2, -0.4, length(uv - vec2(0.3, 0.9)));
    color += sprout * moon * 0.06;
    float vig = smoothstep(1.9, 0.6, length(uv));
    color *= mix(0.72, 1.0, vig);

    gl_FragColor = vec4(color, 1.0);
}
`

function init() {
  const container = containerRef.value
  if (!container) return

  const w = window.innerWidth
  const h = window.innerHeight

  // 渲染器
  renderer = new THREE.WebGLRenderer({ antialias: false })
  renderer.setSize(w, h)
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2))
  container.appendChild(renderer.domElement)

  // 场景 & 正交相机
  scene = new THREE.Scene()
  camera = new THREE.OrthographicCamera(-1, 1, 1, -1, 0, 1)

  // 全屏四边形 + 着色器材质
  const geometry = new THREE.PlaneGeometry(2, 2)
  const material = new THREE.ShaderMaterial({
    vertexShader,
    fragmentShader,
    uniforms: {
      resolution: { value: new THREE.Vector2(w * renderer.getPixelRatio(), h * renderer.getPixelRatio()) },
      time: { value: 0 },
    },
  })
  mesh = new THREE.Mesh(geometry, material)
  scene.add(mesh)

  // 时钟
  clock = new THREE.Clock()

  // 动画循环
  animate()
}

function animate() {
  raf = requestAnimationFrame(animate)
  if (!renderer || !scene || !camera || !mesh || !clock) return

  const material = mesh.material as THREE.ShaderMaterial
  material.uniforms.time.value = clock.getElapsedTime()
  renderer.render(scene, camera)
}

function onResize() {
  if (!renderer || !mesh) return
  const w = window.innerWidth
  const h = window.innerHeight
  renderer.setSize(w, h)
  const pr = renderer.getPixelRatio()
  const material = mesh.material as THREE.ShaderMaterial
  material.uniforms.resolution.value.set(w * pr, h * pr)
}

function cleanup() {
  cancelAnimationFrame(raf)
  window.removeEventListener('resize', onResize)

  if (mesh) {
    mesh.geometry.dispose()
    ;(mesh.material as THREE.ShaderMaterial).dispose()
  }
  if (renderer) {
    renderer.dispose()
    const canvas = renderer.domElement
    if (canvas.parentNode) canvas.parentNode.removeChild(canvas)
  }

  renderer = null
  scene = null
  camera = null
  mesh = null
  clock = null
}

onMounted(() => {
  if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return
  init()
  window.addEventListener('resize', onResize)
})

onBeforeUnmount(() => {
  cleanup()
})
</script>

<template>
  <div ref="containerRef" class="dendro-shader"></div>
</template>

<style scoped>
.dendro-shader {
  position: fixed;
  inset: 0;
  z-index: 0;
  overflow: hidden;
}
</style>

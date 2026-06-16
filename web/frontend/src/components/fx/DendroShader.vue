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

// 须弥草元素配色
vec3 dendro = vec3(0.498, 0.839, 0.314);   // #7fd650
vec3 forest = vec3(0.059, 0.122, 0.090);    // #0f1f17
vec3 wisdom = vec3(0.910, 0.835, 0.639);    // #e8d5a3

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

void main(void) {
    vec2 uv = (gl_FragCoord.xy * 2.0 - resolution.xy) / min(resolution.x, resolution.y);
    float t = time * 0.03;

    // 深绿色基底
    vec3 color = forest;

    // 草叶流动 - 多层噪声叠加
    for(int i = 0; i < 4; i++) {
        float fi = float(i);
        vec2 flow = vec2(
            noise(uv * (2.0 + fi * 0.5) + vec2(t * 0.3, t * 0.1 * fi)),
            noise(uv * (2.0 + fi * 0.5) + vec2(t * 0.1 * fi, t * 0.2))
        );
        float leaf = smoothstep(0.4, 0.6, flow.x * flow.y);
        color = mix(color, dendro * (0.3 + 0.15 * fi), leaf * 0.25);
    }

    // 金色微光点缀
    float sparkle = noise(uv * 8.0 + t * 0.5);
    sparkle = pow(sparkle, 8.0);
    color += wisdom * sparkle * 0.15;

    // 中心柔光
    float glow = 1.0 - length(uv) * 0.5;
    color += dendro * glow * 0.08;

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

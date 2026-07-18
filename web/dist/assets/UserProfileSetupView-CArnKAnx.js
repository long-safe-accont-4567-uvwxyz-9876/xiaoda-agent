import{d as V,o as E,q as D,h as _,c as P,r as y,_ as U,u as F,k as e,B as I,a as k,b as A,i as t,t as i,j as l,m as v,v as m,H as B,F as R,x as T,p as M,f as H,e as N}from"./index-ndYglh2w.js";import{W as G,S as q,b as W,c as K,P as Y,R as X,M as O,C as Z}from"./three.module-Bg_5T8hA.js";import{D as $}from"./DendroEmblem-BnsUKE-E.js";const J=125,z=256,Q=.5,ee=`
#define GLSLIFY 1
attribute vec3 position;
uniform mat4 projectionMatrix;
uniform mat4 modelViewMatrix;
uniform float time;
varying vec3 vPosition;

mat4 rotateMatrixX(float radian) {
  return mat4(
    1.0, 0.0, 0.0, 0.0,
    0.0, cos(radian), -sin(radian), 0.0,
    0.0, sin(radian), cos(radian), 0.0,
    0.0, 0.0, 0.0, 1.0
  );
}

vec3 mod289(vec3 x) { return x - floor(x * (1.0 / 289.0)) * 289.0; }
vec4 mod289(vec4 x) { return x - floor(x * (1.0 / 289.0)) * 289.0; }
vec4 permute(vec4 x) { return mod289(((x*34.0)+1.0)*x); }
vec4 taylorInvSqrt(vec4 r) { return 1.79284291400159 - 0.85373472095314 * r; }
vec3 fade(vec3 t) { return t*t*t*(t*(t*6.0-15.0)+10.0); }

float cnoise(vec3 P) {
  vec3 Pi0 = floor(P);
  vec3 Pi1 = Pi0 + vec3(1.0);
  Pi0 = mod289(Pi0);
  Pi1 = mod289(Pi1);
  vec3 Pf0 = fract(P);
  vec3 Pf1 = Pf0 - vec3(1.0);
  vec4 ix = vec4(Pi0.x, Pi1.x, Pi0.x, Pi1.x);
  vec4 iy = vec4(Pi0.yy, Pi1.yy);
  vec4 iz0 = Pi0.zzzz;
  vec4 iz1 = Pi1.zzzz;

  vec4 ixy = permute(permute(ix) + iy);
  vec4 ixy0 = permute(ixy + iz0);
  vec4 ixy1 = permute(ixy + iz1);

  vec4 gx0 = ixy0 * (1.0 / 7.0);
  vec4 gy0 = fract(floor(gx0) * (1.0 / 7.0)) - 0.5;
  gx0 = fract(gx0);
  vec4 gz0 = vec4(0.5) - abs(gx0) - abs(gy0);
  vec4 sz0 = step(gz0, vec4(0.0));
  gx0 -= sz0 * (step(0.0, gx0) - 0.5);
  gy0 -= sz0 * (step(0.0, gy0) - 0.5);

  vec4 gx1 = ixy1 * (1.0 / 7.0);
  vec4 gy1 = fract(floor(gx1) * (1.0 / 7.0)) - 0.5;
  gx1 = fract(gx1);
  vec4 gz1 = vec4(0.5) - abs(gx1) - abs(gy1);
  vec4 sz1 = step(gz1, vec4(0.0));
  gx1 -= sz1 * (step(0.0, gx1) - 0.5);
  gy1 -= sz1 * (step(0.0, gy1) - 0.5);

  vec3 g000 = vec3(gx0.x,gy0.x,gz0.x);
  vec3 g100 = vec3(gx0.y,gy0.y,gz0.y);
  vec3 g010 = vec3(gx0.z,gy0.z,gz0.z);
  vec3 g110 = vec3(gx0.w,gy0.w,gz0.w);
  vec3 g001 = vec3(gx1.x,gy1.x,gz1.x);
  vec3 g101 = vec3(gx1.y,gy1.y,gz1.y);
  vec3 g011 = vec3(gx1.z,gy1.z,gz1.z);
  vec3 g111 = vec3(gx1.w,gy1.w,gz1.w);

  vec4 norm0 = taylorInvSqrt(vec4(dot(g000, g000), dot(g010, g010), dot(g100, g100), dot(g110, g110)));
  g000 *= norm0.x;
  g010 *= norm0.y;
  g100 *= norm0.z;
  g110 *= norm0.w;
  vec4 norm1 = taylorInvSqrt(vec4(dot(g001, g001), dot(g011, g011), dot(g101, g101), dot(g111, g111)));
  g001 *= norm1.x;
  g011 *= norm1.y;
  g101 *= norm1.z;
  g111 *= norm1.w;

  float n000 = dot(g000, Pf0);
  float n100 = dot(g100, vec3(Pf1.x, Pf0.yz));
  float n010 = dot(g010, vec3(Pf0.x, Pf1.y, Pf0.z));
  float n110 = dot(g110, vec3(Pf1.xy, Pf0.z));
  float n001 = dot(g001, vec3(Pf0.xy, Pf1.z));
  float n101 = dot(g101, vec3(Pf1.x, Pf0.y, Pf1.z));
  float n011 = dot(g011, vec3(Pf0.x, Pf1.yz));
  float n111 = dot(g111, Pf1);

  vec3 fade_xyz = fade(Pf0);
  vec4 n_z = mix(vec4(n000, n100, n010, n110), vec4(n001, n101, n011, n111), fade_xyz.z);
  vec2 n_yz = mix(n_z.xy, n_z.zw, fade_xyz.y);
  float n_xyz = mix(n_yz.x, n_yz.y, fade_xyz.x);
  return 2.2 * n_xyz;
}

void main(void) {
  vec3 updatePosition = (rotateMatrixX(radians(90.0)) * vec4(position, 1.0)).xyz;
  float sin1 = sin(radians(updatePosition.x / 128.0 * 90.0));
  vec3 noisePosition = updatePosition + vec3(0.0, 0.0, time * -30.0);
  float noise1 = cnoise(noisePosition * 0.08);
  float noise2 = cnoise(noisePosition * 0.06);
  float noise3 = cnoise(noisePosition * 0.4);
  vec3 lastPosition = updatePosition + vec3(0.0,
    noise1 * sin1 * 8.0
    + noise2 * sin1 * 8.0
    + noise3 * (abs(sin1) * 2.0 + 0.5)
    + pow(sin1, 2.0) * 40.0, 0.0);

  vPosition = lastPosition;
  gl_Position = projectionMatrix * modelViewMatrix * vec4(lastPosition, 1.0);
}
`,te=`
precision highp float;
#define GLSLIFY 1
varying vec3 vPosition;

// 须弥草元素配色
vec3 dendro = vec3(0.498, 0.839, 0.314);   // #7fd650
vec3 forest = vec3(0.114, 0.231, 0.165);    // #1d3b2a
vec3 wisdom = vec3(0.910, 0.835, 0.639);    // #e8d5a3

void main(void) {
  float opacity = (96.0 - length(vPosition)) / 256.0 * 0.6;
  // 混合草元素绿和森林深绿
  float depth = clamp(length(vPosition) / 128.0, 0.0, 1.0);
  vec3 color = mix(dendro * 0.4, forest, depth);
  // 添加金色微光
  color += wisdom * 0.05 * (1.0 - depth);
  gl_FragColor = vec4(color, opacity);
}
`,oe=V({__name:"GlslHills",setup(C){const x=y(null);let r=null,d=null,u=null,p=null,S=0,f=null,s=null,h=0;function b(){const c=x.value;if(!c)return;const g=window.innerWidth,w=window.innerHeight;r=new G({antialias:!1,alpha:!0}),r.setSize(g,w),r.setPixelRatio(Math.min(window.devicePixelRatio,2)),r.setClearColor(0,0),c.appendChild(r.domElement),d=new q,u=new W(45,g/w,1,1e4),u.position.set(0,16,J),u.lookAt(new K(0,28,0)),s={time:{value:0}};const L=new Y(z,z,z,z),j=new X({uniforms:s,vertexShader:ee,fragmentShader:te,transparent:!0});p=new O(L,j),d.add(p),f=new Z,n()}function n(){if(S=requestAnimationFrame(n),!r||!d||!u||!p||!f||!s)return;const c=f.getDelta();h+=c*Q,s.time.value=h,r.render(d,u)}function o(){if(!r||!u)return;const c=window.innerWidth,g=window.innerHeight;r.setSize(c,g),u.aspect=c/g,u.updateProjectionMatrix()}function a(){if(cancelAnimationFrame(S),window.removeEventListener("resize",o),p&&(p.geometry.dispose(),p.material.dispose()),r){r.dispose();const c=r.domElement;c.parentNode&&c.parentNode.removeChild(c)}r=null,d=null,u=null,p=null,f=null,s=null}return E(()=>{window.matchMedia("(prefers-reduced-motion: reduce)").matches||(b(),window.addEventListener("resize",o))}),D(()=>{a()}),(c,g)=>(_(),P("div",{ref_key:"containerRef",ref:x,class:"glsl-hills"},null,512))}}),se=U(oe,[["__scopeId","data-v-aa888599"]]),le={class:"setup-page"},ae={class:"setup-center"},ie={class:"setup-card glass-panel"},re={class:"setup-header"},ne={class:"subtitle"},ue={class:"version-tag"},ce={class:"setup-body"},de={class:"section-title"},pe={class:"form-group"},ve={class:"form-label"},fe=["placeholder"],me={class:"form-group"},ge={class:"form-label"},_e=["placeholder"],Pe={class:"form-group"},ye={class:"form-label"},xe=["value"],Se={class:"section-title section-gap"},he={class:"form-group"},ze={class:"form-label"},be=["placeholder"],we={class:"form-group"},ke={class:"form-label"},Ae=["placeholder"],Me={class:"section-title section-gap"},Ve={class:"form-group"},Ee={class:"form-label"},Ue=["placeholder"],Ce={class:"form-group"},Le={class:"form-label"},je=["placeholder"],De={class:"section-title section-gap"},Fe={class:"form-group"},Ie={class:"form-label"},Be=["placeholder"],Re={key:0,class:"error-text"},Te={key:1,class:"success-text"},He={class:"action-row"},Ne=["disabled"],Ge=["disabled"],qe={class:"status-hint"},We=V({__name:"UserProfileSetupView",setup(C){const x=N();F();const r=y("dev"),d=y(!1),u=y(""),p=y(!1),S=H(()=>[{value:"Asia/Shanghai",label:e("userProfileSetup.timezones.Asia/Shanghai")},{value:"Asia/Hong_Kong",label:e("userProfileSetup.timezones.Asia/Hong_Kong")},{value:"Asia/Taipei",label:e("userProfileSetup.timezones.Asia/Taipei")},{value:"Asia/Tokyo",label:e("userProfileSetup.timezones.Asia/Tokyo")},{value:"Asia/Seoul",label:e("userProfileSetup.timezones.Asia/Seoul")},{value:"Asia/Singapore",label:e("userProfileSetup.timezones.Asia/Singapore")},{value:"Asia/Bangkok",label:e("userProfileSetup.timezones.Asia/Bangkok")},{value:"Asia/Kolkata",label:e("userProfileSetup.timezones.Asia/Kolkata")},{value:"Asia/Dubai",label:e("userProfileSetup.timezones.Asia/Dubai")},{value:"Europe/London",label:e("userProfileSetup.timezones.Europe/London")},{value:"Europe/Paris",label:e("userProfileSetup.timezones.Europe/Paris")},{value:"Europe/Berlin",label:e("userProfileSetup.timezones.Europe/Berlin")},{value:"Europe/Moscow",label:e("userProfileSetup.timezones.Europe/Moscow")},{value:"America/New_York",label:e("userProfileSetup.timezones.America/New_York")},{value:"America/Chicago",label:e("userProfileSetup.timezones.America/Chicago")},{value:"America/Denver",label:e("userProfileSetup.timezones.America/Denver")},{value:"America/Los_Angeles",label:e("userProfileSetup.timezones.America/Los_Angeles")},{value:"America/Sao_Paulo",label:e("userProfileSetup.timezones.America/Sao_Paulo")},{value:"Australia/Sydney",label:e("userProfileSetup.timezones.Australia/Sydney")},{value:"Pacific/Auckland",label:e("userProfileSetup.timezones.Pacific/Auckland")}]),f={address_term:"",name:"",device:"",timezone:"Asia/Shanghai",preferred_personality:e("userProfileSetup.defaultPersonality"),preferred_tone:e("userProfileSetup.defaultTone"),like_to_be_called:"",liked_reply_style:e("userProfileSetup.defaultLiked"),disliked_reply_style:e("userProfileSetup.defaultDisliked"),project_preferences:e("userProfileSetup.defaultPrefs"),history_notes:""},s=y({...f});E(async()=>{try{const n=await I();r.value=n.version||"dev"}catch{}try{const n=await k.getSetupUserProfile();for(const o of Object.keys(f))n[o]!==void 0&&n[o]!==""&&(s.value[o]=n[o])}catch(n){console.error("[UserProfileSetup] load failed:",n)}});async function h(){d.value=!0,u.value="",p.value=!1;try{const n={...s.value,address_term:s.value.address_term.trim()||e("userProfileSetup.defaultFriend"),name:s.value.name.trim()||"User",like_to_be_called:s.value.address_term.trim()||e("userProfileSetup.defaultFriend")};await k.saveSetupUserProfile(n),localStorage.setItem("xiaoda_profile_done","true"),p.value=!0,setTimeout(()=>{x.replace("/")},1200)}catch(n){u.value=n.message||e("userProfileSetup.saveFailed")}finally{d.value=!1}}async function b(){localStorage.setItem("xiaoda_profile_done","true"),x.replace("/")}return(n,o)=>(_(),P("div",le,[A(se),t("div",ae,[t("div",ie,[o[8]||(o[8]=t("span",{class:"vine corner-tl"},null,-1)),o[9]||(o[9]=t("span",{class:"vine corner-br"},null,-1)),t("div",re,[A($,{size:84,spin:""}),t("h1",null,i(l(e)("userProfileSetup.title")),1),t("p",ne,i(l(e)("userProfileSetup.greeting")),1),t("p",ue,"v"+i(r.value),1)]),t("div",ce,[t("h2",de,"── "+i(l(e)("userProfileSetup.userInfo"))+" ──",1),t("div",pe,[t("label",ve,i(l(e)("userProfileSetup.addressTerm")),1),v(t("input",{"onUpdate:modelValue":o[0]||(o[0]=a=>s.value.address_term=a),class:"dendro-input",type:"text",placeholder:l(e)("userProfileSetup.addressEmptyDefault")},null,8,fe),[[m,s.value.address_term]])]),t("div",me,[t("label",ge,i(l(e)("userProfileSetup.nickname")),1),v(t("input",{"onUpdate:modelValue":o[1]||(o[1]=a=>s.value.name=a),class:"dendro-input",type:"text",placeholder:l(e)("userProfileSetup.nicknamePh")},null,8,_e),[[m,s.value.name]])]),t("div",Pe,[t("label",ye,i(l(e)("userProfileSetup.timezone")),1),v(t("select",{"onUpdate:modelValue":o[2]||(o[2]=a=>s.value.timezone=a),class:"dendro-input dendro-select"},[(_(!0),P(R,null,T(S.value,a=>(_(),P("option",{key:a.value,value:a.value},i(a.label),9,xe))),128))],512),[[B,s.value.timezone]])]),t("h2",Se,"── "+i(l(e)("userProfileSetup.agentPersonality"))+" ──",1),t("div",he,[t("label",ze,i(l(e)("userProfileSetup.preferredPersonality")),1),v(t("input",{"onUpdate:modelValue":o[3]||(o[3]=a=>s.value.preferred_personality=a),class:"dendro-input",type:"text",placeholder:l(e)("userProfileSetup.personalityPh")},null,8,be),[[m,s.value.preferred_personality]])]),t("div",we,[t("label",ke,i(l(e)("userProfileSetup.preferredTone")),1),v(t("input",{"onUpdate:modelValue":o[4]||(o[4]=a=>s.value.preferred_tone=a),class:"dendro-input",type:"text",placeholder:l(e)("userProfileSetup.tonePh")},null,8,Ae),[[m,s.value.preferred_tone]])]),t("h2",Me,"── "+i(l(e)("userProfileSetup.replyPrefs"))+" ──",1),t("div",Ve,[t("label",Ee,i(l(e)("userProfileSetup.likedStyle")),1),v(t("textarea",{"onUpdate:modelValue":o[5]||(o[5]=a=>s.value.liked_reply_style=a),class:"dendro-input dendro-textarea",placeholder:l(e)("userProfileSetup.likedPh"),rows:"2"},null,8,Ue),[[m,s.value.liked_reply_style]])]),t("div",Ce,[t("label",Le,i(l(e)("userProfileSetup.dislikedStyle")),1),v(t("textarea",{"onUpdate:modelValue":o[6]||(o[6]=a=>s.value.disliked_reply_style=a),class:"dendro-input dendro-textarea",placeholder:l(e)("userProfileSetup.dislikedPh"),rows:"2"},null,8,je),[[m,s.value.disliked_reply_style]])]),t("h2",De,"── "+i(l(e)("userProfileSetup.projectPrefs"))+" ──",1),t("div",Fe,[t("label",Ie,i(l(e)("userProfileSetup.projectPrefs")),1),v(t("textarea",{"onUpdate:modelValue":o[7]||(o[7]=a=>s.value.project_preferences=a),class:"dendro-input dendro-textarea",placeholder:l(e)("userProfileSetup.projectPrefsPh"),rows:"5"},null,8,Be),[[m,s.value.project_preferences]])]),u.value?(_(),P("p",Re,i(u.value),1)):M("",!0),p.value?(_(),P("p",Te,i(l(e)("userProfileSetup.savedSuccess")),1)):M("",!0),t("div",He,[t("button",{class:"dendro-btn skip-btn",onClick:b,disabled:d.value},i(l(e)("userProfileSetup.skip")),9,Ne),t("button",{class:"dendro-btn save-btn",disabled:d.value,onClick:h},i(d.value?l(e)("setupWizard.saving"):l(e)("userProfileSetup.saveEnter")),9,Ge)]),t("p",qe,i(l(e)("userProfileSetup.infoHint")),1)])])])]))}}),Oe=U(We,[["__scopeId","data-v-7683c128"]]);export{Oe as default};

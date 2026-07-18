import{d as E,R as i,b3 as ye,f as I,ba as be,bb as we,bc as xe,bd as _e,be as De,a7 as J,a9 as w,aa as O,Y as Ie,af as ke,ah as qe,bf as Te,bg as pe,N as We,o as Ae,g as L,q as Oe,c as N,i as d,t as f,j as a,b as m,w as b,F as ie,x as oe,p as W,K as je,r as P,h as C,k as s,au as F,z as T,aw as Z,y as Me,I as se,as as ge,a as Ge,b8 as Ue,b9 as le,_ as Le}from"./index-ndYglh2w.js";import{T as G}from"./Tilt3D-C2KnzATB.js";import{f as H}from"./Popover-C334eSt7.js";import{u as Fe}from"./use-message-B5GKdb0p.js";import{N as X,a as fe}from"./Tabs-gjLWxwhU.js";import{N as ne}from"./Input-LoMiPmjF.js";import{N as ee}from"./Select-BMdHcLcc.js";import{N as He}from"./Switch-CVUGIKjj.js";import{N as me}from"./Tag-eufxZD1O.js";import{N as ve}from"./Popconfirm-qdzYoVyL.js";import"./Add-B97VbVvF.js";import"./Suffix-BRyuY6lw.js";import"./FocusDetector-D_ISBkH5.js";import"./Empty-BH-rKQkK.js";const Xe={success:i(_e,null),error:i(xe,null),warning:i(we,null),info:i(be,null)},Ye=E({name:"ProgressCircle",props:{clsPrefix:{type:String,required:!0},status:{type:String,required:!0},strokeWidth:{type:Number,required:!0},fillColor:[String,Object],railColor:String,railStyle:[String,Object],percentage:{type:Number,default:0},offsetDegree:{type:Number,default:0},showIndicator:{type:Boolean,required:!0},indicatorTextColor:String,unit:String,viewBoxWidth:{type:Number,required:!0},gapDegree:{type:Number,required:!0},gapOffsetDegree:{type:Number,default:0}},setup(r,{slots:u}){const _=I(()=>{const n="gradient",{fillColor:l}=r;return typeof l=="object"?`${n}-${De(JSON.stringify(l))}`:n});function z(n,l,g,x){const{gapDegree:k,viewBoxWidth:$,strokeWidth:S}=r,v=50,h=0,p=v,c=0,q=2*v,D=50+S/2,V=`M ${D},${D} m ${h},${p}
      a ${v},${v} 0 1 1 ${c},${-q}
      a ${v},${v} 0 1 1 ${-c},${q}`,B=Math.PI*2*v,R={stroke:x==="rail"?g:typeof r.fillColor=="object"?`url(#${_.value})`:g,strokeDasharray:`${Math.min(n,100)/100*(B-k)}px ${$*8}px`,strokeDashoffset:`-${k/2}px`,transformOrigin:l?"center":void 0,transform:l?`rotate(${l}deg)`:void 0};return{pathString:V,pathStyle:R}}const y=()=>{const n=typeof r.fillColor=="object",l=n?r.fillColor.stops[0]:"",g=n?r.fillColor.stops[1]:"";return n&&i("defs",null,i("linearGradient",{id:_.value,x1:"0%",y1:"100%",x2:"100%",y2:"0%"},i("stop",{offset:"0%","stop-color":l}),i("stop",{offset:"100%","stop-color":g})))};return()=>{const{fillColor:n,railColor:l,strokeWidth:g,offsetDegree:x,status:k,percentage:$,showIndicator:S,indicatorTextColor:v,unit:h,gapOffsetDegree:p,clsPrefix:c}=r,{pathString:q,pathStyle:D}=z(100,0,l,"rail"),{pathString:V,pathStyle:B}=z($,x,n,"fill"),R=100+g;return i("div",{class:`${c}-progress-content`,role:"none"},i("div",{class:`${c}-progress-graph`,"aria-hidden":!0},i("div",{class:`${c}-progress-graph-circle`,style:{transform:p?`rotate(${p}deg)`:void 0}},i("svg",{viewBox:`0 0 ${R} ${R}`},y(),i("g",null,i("path",{class:`${c}-progress-graph-circle-rail`,d:q,"stroke-width":g,"stroke-linecap":"round",fill:"none",style:D})),i("g",null,i("path",{class:[`${c}-progress-graph-circle-fill`,$===0&&`${c}-progress-graph-circle-fill--empty`],d:V,"stroke-width":g,"stroke-linecap":"round",fill:"none",style:B}))))),S?i("div",null,u.default?i("div",{class:`${c}-progress-custom-content`,role:"none"},u.default()):k!=="default"?i("div",{class:`${c}-progress-icon`,"aria-hidden":!0},i(ye,{clsPrefix:c},{default:()=>Xe[k]})):i("div",{class:`${c}-progress-text`,style:{color:v},role:"none"},i("span",{class:`${c}-progress-text__percentage`},$),i("span",{class:`${c}-progress-text__unit`},h))):null)}}}),Ee={success:i(_e,null),error:i(xe,null),warning:i(we,null),info:i(be,null)},Ke=E({name:"ProgressLine",props:{clsPrefix:{type:String,required:!0},percentage:{type:Number,default:0},railColor:String,railStyle:[String,Object],fillColor:[String,Object],status:{type:String,required:!0},indicatorPlacement:{type:String,required:!0},indicatorTextColor:String,unit:{type:String,default:"%"},processing:{type:Boolean,required:!0},showIndicator:{type:Boolean,required:!0},height:[String,Number],railBorderRadius:[String,Number],fillBorderRadius:[String,Number]},setup(r,{slots:u}){const _=I(()=>H(r.height)),z=I(()=>{var l,g;return typeof r.fillColor=="object"?`linear-gradient(to right, ${(l=r.fillColor)===null||l===void 0?void 0:l.stops[0]} , ${(g=r.fillColor)===null||g===void 0?void 0:g.stops[1]})`:r.fillColor}),y=I(()=>r.railBorderRadius!==void 0?H(r.railBorderRadius):r.height!==void 0?H(r.height,{c:.5}):""),n=I(()=>r.fillBorderRadius!==void 0?H(r.fillBorderRadius):r.railBorderRadius!==void 0?H(r.railBorderRadius):r.height!==void 0?H(r.height,{c:.5}):"");return()=>{const{indicatorPlacement:l,railColor:g,railStyle:x,percentage:k,unit:$,indicatorTextColor:S,status:v,showIndicator:h,processing:p,clsPrefix:c}=r;return i("div",{class:`${c}-progress-content`,role:"none"},i("div",{class:`${c}-progress-graph`,"aria-hidden":!0},i("div",{class:[`${c}-progress-graph-line`,{[`${c}-progress-graph-line--indicator-${l}`]:!0}]},i("div",{class:`${c}-progress-graph-line-rail`,style:[{backgroundColor:g,height:_.value,borderRadius:y.value},x]},i("div",{class:[`${c}-progress-graph-line-fill`,p&&`${c}-progress-graph-line-fill--processing`],style:{maxWidth:`${r.percentage}%`,background:z.value,height:_.value,lineHeight:_.value,borderRadius:n.value}},l==="inside"?i("div",{class:`${c}-progress-graph-line-indicator`,style:{color:S}},u.default?u.default():`${k}${$}`):null)))),h&&l==="outside"?i("div",null,u.default?i("div",{class:`${c}-progress-custom-content`,style:{color:S},role:"none"},u.default()):v==="default"?i("div",{role:"none",class:`${c}-progress-icon ${c}-progress-icon--as-text`,style:{color:S}},k,$):i("div",{class:`${c}-progress-icon`,"aria-hidden":!0},i(ye,{clsPrefix:c},{default:()=>Ee[v]}))):null)}}});function he(r,u,_=100){return`m ${_/2} ${_/2-r} a ${r} ${r} 0 1 1 0 ${2*r} a ${r} ${r} 0 1 1 0 -${2*r}`}const Qe=E({name:"ProgressMultipleCircle",props:{clsPrefix:{type:String,required:!0},viewBoxWidth:{type:Number,required:!0},percentage:{type:Array,default:[0]},strokeWidth:{type:Number,required:!0},circleGap:{type:Number,required:!0},showIndicator:{type:Boolean,required:!0},fillColor:{type:Array,default:()=>[]},railColor:{type:Array,default:()=>[]},railStyle:{type:Array,default:()=>[]}},setup(r,{slots:u}){const _=I(()=>r.percentage.map((n,l)=>`${Math.PI*n/100*(r.viewBoxWidth/2-r.strokeWidth/2*(1+2*l)-r.circleGap*l)*2}, ${r.viewBoxWidth*8}`)),z=(y,n)=>{const l=r.fillColor[n],g=typeof l=="object"?l.stops[0]:"",x=typeof l=="object"?l.stops[1]:"";return typeof r.fillColor[n]=="object"&&i("linearGradient",{id:`gradient-${n}`,x1:"100%",y1:"0%",x2:"0%",y2:"100%"},i("stop",{offset:"0%","stop-color":g}),i("stop",{offset:"100%","stop-color":x}))};return()=>{const{viewBoxWidth:y,strokeWidth:n,circleGap:l,showIndicator:g,fillColor:x,railColor:k,railStyle:$,percentage:S,clsPrefix:v}=r;return i("div",{class:`${v}-progress-content`,role:"none"},i("div",{class:`${v}-progress-graph`,"aria-hidden":!0},i("div",{class:`${v}-progress-graph-circle`},i("svg",{viewBox:`0 0 ${y} ${y}`},i("defs",null,S.map((h,p)=>z(h,p))),S.map((h,p)=>i("g",{key:p},i("path",{class:`${v}-progress-graph-circle-rail`,d:he(y/2-n/2*(1+2*p)-l*p,n,y),"stroke-width":n,"stroke-linecap":"round",fill:"none",style:[{strokeDashoffset:0,stroke:k[p]},$[p]]}),i("path",{class:[`${v}-progress-graph-circle-fill`,h===0&&`${v}-progress-graph-circle-fill--empty`],d:he(y/2-n/2*(1+2*p)-l*p,n,y),"stroke-width":n,"stroke-linecap":"round",fill:"none",style:{strokeDasharray:_.value[p],strokeDashoffset:0,stroke:typeof x[p]=="object"?`url(#gradient-${p})`:x[p]}})))))),g&&u.default?i("div",null,i("div",{class:`${v}-progress-text`},u.default())):null)}}}),Je=J([w("progress",{display:"inline-block"},[w("progress-icon",`
 color: var(--n-icon-color);
 transition: color .3s var(--n-bezier);
 `),O("line",`
 width: 100%;
 display: block;
 `,[w("progress-content",`
 display: flex;
 align-items: center;
 `,[w("progress-graph",{flex:1})]),w("progress-custom-content",{marginLeft:"14px"}),w("progress-icon",`
 width: 30px;
 padding-left: 14px;
 height: var(--n-icon-size-line);
 line-height: var(--n-icon-size-line);
 font-size: var(--n-icon-size-line);
 `,[O("as-text",`
 color: var(--n-text-color-line-outer);
 text-align: center;
 width: 40px;
 font-size: var(--n-font-size);
 padding-left: 4px;
 transition: color .3s var(--n-bezier);
 `)])]),O("circle, dashboard",{width:"120px"},[w("progress-custom-content",`
 position: absolute;
 left: 50%;
 top: 50%;
 transform: translateX(-50%) translateY(-50%);
 display: flex;
 align-items: center;
 justify-content: center;
 `),w("progress-text",`
 position: absolute;
 left: 50%;
 top: 50%;
 transform: translateX(-50%) translateY(-50%);
 display: flex;
 align-items: center;
 color: inherit;
 font-size: var(--n-font-size-circle);
 color: var(--n-text-color-circle);
 font-weight: var(--n-font-weight-circle);
 transition: color .3s var(--n-bezier);
 white-space: nowrap;
 `),w("progress-icon",`
 position: absolute;
 left: 50%;
 top: 50%;
 transform: translateX(-50%) translateY(-50%);
 display: flex;
 align-items: center;
 color: var(--n-icon-color);
 font-size: var(--n-icon-size-circle);
 `)]),O("multiple-circle",`
 width: 200px;
 color: inherit;
 `,[w("progress-text",`
 font-weight: var(--n-font-weight-circle);
 color: var(--n-text-color-circle);
 position: absolute;
 left: 50%;
 top: 50%;
 transform: translateX(-50%) translateY(-50%);
 display: flex;
 align-items: center;
 justify-content: center;
 transition: color .3s var(--n-bezier);
 `)]),w("progress-content",{position:"relative"}),w("progress-graph",{position:"relative"},[w("progress-graph-circle",[J("svg",{verticalAlign:"bottom"}),w("progress-graph-circle-fill",`
 stroke: var(--n-fill-color);
 transition:
 opacity .3s var(--n-bezier),
 stroke .3s var(--n-bezier),
 stroke-dasharray .3s var(--n-bezier);
 `,[O("empty",{opacity:0})]),w("progress-graph-circle-rail",`
 transition: stroke .3s var(--n-bezier);
 overflow: hidden;
 stroke: var(--n-rail-color);
 `)]),w("progress-graph-line",[O("indicator-inside",[w("progress-graph-line-rail",`
 height: 16px;
 line-height: 16px;
 border-radius: 10px;
 `,[w("progress-graph-line-fill",`
 height: inherit;
 border-radius: 10px;
 `),w("progress-graph-line-indicator",`
 background: #0000;
 white-space: nowrap;
 text-align: right;
 margin-left: 14px;
 margin-right: 14px;
 height: inherit;
 font-size: 12px;
 color: var(--n-text-color-line-inner);
 transition: color .3s var(--n-bezier);
 `)])]),O("indicator-inside-label",`
 height: 16px;
 display: flex;
 align-items: center;
 `,[w("progress-graph-line-rail",`
 flex: 1;
 transition: background-color .3s var(--n-bezier);
 `),w("progress-graph-line-indicator",`
 background: var(--n-fill-color);
 font-size: 12px;
 transform: translateZ(0);
 display: flex;
 vertical-align: middle;
 height: 16px;
 line-height: 16px;
 padding: 0 10px;
 border-radius: 10px;
 position: absolute;
 white-space: nowrap;
 color: var(--n-text-color-line-inner);
 transition:
 right .2s var(--n-bezier),
 color .3s var(--n-bezier),
 background-color .3s var(--n-bezier);
 `)]),w("progress-graph-line-rail",`
 position: relative;
 overflow: hidden;
 height: var(--n-rail-height);
 border-radius: 5px;
 background-color: var(--n-rail-color);
 transition: background-color .3s var(--n-bezier);
 `,[w("progress-graph-line-fill",`
 background: var(--n-fill-color);
 position: relative;
 border-radius: 5px;
 height: inherit;
 width: 100%;
 max-width: 0%;
 transition:
 background-color .3s var(--n-bezier),
 max-width .2s var(--n-bezier);
 `,[O("processing",[J("&::after",`
 content: "";
 background-image: var(--n-line-bg-processing);
 animation: progress-processing-animation 2s var(--n-bezier) infinite;
 `)])])])])])]),J("@keyframes progress-processing-animation",`
 0% {
 position: absolute;
 left: 0;
 top: 0;
 bottom: 0;
 right: 100%;
 opacity: 1;
 }
 66% {
 position: absolute;
 left: 0;
 top: 0;
 bottom: 0;
 right: 0;
 opacity: 0;
 }
 100% {
 position: absolute;
 left: 0;
 top: 0;
 bottom: 0;
 right: 0;
 opacity: 0;
 }
 `)]),Ze=Object.assign(Object.assign({},ke.props),{processing:Boolean,type:{type:String,default:"line"},gapDegree:Number,gapOffsetDegree:Number,status:{type:String,default:"default"},railColor:[String,Array],railStyle:[String,Array],color:[String,Array,Object],viewBoxWidth:{type:Number,default:100},strokeWidth:{type:Number,default:7},percentage:[Number,Array],unit:{type:String,default:"%"},showIndicator:{type:Boolean,default:!0},indicatorPosition:{type:String,default:"outside"},indicatorPlacement:{type:String,default:"outside"},indicatorTextColor:String,circleGap:{type:Number,default:1},height:Number,borderRadius:[String,Number],fillBorderRadius:[String,Number],offsetDegree:Number}),et=E({name:"Progress",props:Ze,setup(r){const u=I(()=>r.indicatorPlacement||r.indicatorPosition),_=I(()=>{if(r.gapDegree||r.gapDegree===0)return r.gapDegree;if(r.type==="dashboard")return 75}),{mergedClsPrefixRef:z,inlineThemeDisabled:y}=Ie(r),n=ke("Progress","-progress",Je,Te,r,z),l=I(()=>{const{status:x}=r,{common:{cubicBezierEaseInOut:k},self:{fontSize:$,fontSizeCircle:S,railColor:v,railHeight:h,iconSizeCircle:p,iconSizeLine:c,textColorCircle:q,textColorLineInner:D,textColorLineOuter:V,lineBgProcessing:B,fontWeightCircle:R,[pe("iconColor",x)]:U,[pe("fillColor",x)]:j}}=n.value;return{"--n-bezier":k,"--n-fill-color":j,"--n-font-size":$,"--n-font-size-circle":S,"--n-font-weight-circle":R,"--n-icon-color":U,"--n-icon-size-circle":p,"--n-icon-size-line":c,"--n-line-bg-processing":B,"--n-rail-color":v,"--n-rail-height":h,"--n-text-color-circle":q,"--n-text-color-line-inner":D,"--n-text-color-line-outer":V}}),g=y?qe("progress",I(()=>r.status[0]),l,r):void 0;return{mergedClsPrefix:z,mergedIndicatorPlacement:u,gapDeg:_,cssVars:y?void 0:l,themeClass:g==null?void 0:g.themeClass,onRender:g==null?void 0:g.onRender}},render(){const{type:r,cssVars:u,indicatorTextColor:_,showIndicator:z,status:y,railColor:n,railStyle:l,color:g,percentage:x,viewBoxWidth:k,strokeWidth:$,mergedIndicatorPlacement:S,unit:v,borderRadius:h,fillBorderRadius:p,height:c,processing:q,circleGap:D,mergedClsPrefix:V,gapDeg:B,gapOffsetDegree:R,themeClass:U,$slots:j,onRender:Y}=this;return Y==null||Y(),i("div",{class:[U,`${V}-progress`,`${V}-progress--${r}`,`${V}-progress--${y}`],style:u,"aria-valuemax":100,"aria-valuemin":0,"aria-valuenow":x,role:r==="circle"||r==="line"||r==="dashboard"?"progressbar":"none"},r==="circle"||r==="dashboard"?i(Ye,{clsPrefix:V,status:y,showIndicator:z,indicatorTextColor:_,railColor:n,fillColor:g,railStyle:l,offsetDegree:this.offsetDegree,percentage:x,viewBoxWidth:k,strokeWidth:$,gapDegree:B===void 0?r==="dashboard"?75:0:B,gapOffsetDegree:R,unit:v},j):r==="line"?i(Ke,{clsPrefix:V,status:y,showIndicator:z,indicatorTextColor:_,railColor:n,fillColor:g,railStyle:l,percentage:x,processing:q,indicatorPlacement:S,unit:v,fillBorderRadius:p,railBorderRadius:h,height:c},j):r==="multiple-circle"?i(Qe,{clsPrefix:V,strokeWidth:$,railColor:n,fillColor:g,railStyle:l,viewBoxWidth:k,percentage:x,showIndicator:z,circleGap:D},j):null)}}),tt={class:"media-view"},at={class:"view-title"},rt={class:"panel-row"},it={class:"glass-panel panel main"},ot={class:"tts-controls"},st=["src"],lt={class:"glass-panel panel side"},nt={class:"cfg"},ct={class:"cfg-hint"},dt={class:"glass-panel panel"},ut={class:"cfg-hint",style:{"margin-bottom":"12px"}},pt={class:"voice-select-row",style:{"margin-bottom":"14px"}},gt={key:0,class:"voice-agent-block"},ft={class:"voice-agent-header"},mt={class:"voice-agent-name"},vt={class:"voice-agent-current"},ht={class:"voice-agent-body"},yt={class:"tts-controls",style:{"margin-bottom":"8px"}},bt={class:"voice-select-row"},wt={class:"voice-list"},xt={key:0,class:"empty-hint",style:{padding:"4px 0","text-align":"left"}},_t={class:"glass-panel panel"},kt={class:"glass-panel panel"},$t={class:"queue-hint"},Ct={class:"glass-panel section"},St={class:"task-list"},Vt={class:"task-kind"},Pt={class:"task-prompt"},zt={key:1,class:"task-error"},Nt=["href"],Bt={key:0,class:"empty-hint"},Rt={class:"glass-panel section"},Dt={class:"gallery-head"},It={class:"gallery-grid"},qt={class:"gallery-card"},Tt=["src","onClick"],Wt=["src"],At=["src"],Ot={class:"gallery-meta"},jt={class:"g-name"},Mt={key:0,class:"empty-hint"},Gt=E({__name:"MediaView",setup(r){const u=Fe(),_=We(),z=je(),y=P(""),n=P("xiaoda"),l=P(null),g=P({}),x=P([]),k=P(""),$=P(!1),S=P(!1),v=P(null),h=P(""),p=P([]),c=P(""),q=P(""),D=P(""),V=P([]),B=P("image"),R=P([]);Ae(async()=>{var o;try{const t=await L("/agents");p.value=t.map(e=>({name:e.name,display_name:e.display_name||e.name,voice_ref:e.voice_ref})),p.value.length&&(h.value=p.value[0].name)}catch{}await te();try{const t=await L("/media/tts/config");_.autoSpeak=t.auto_speak,(!n.value||!p.value.find(e=>e.name===n.value))&&(n.value=((o=p.value[0])==null?void 0:o.name)||"xiaoda")}catch{}K(),Q(),z.on("media_task_update",U)}),Oe(()=>z.off("media_task_update",U));function U(o){const t=V.value.find(e=>e.id===o.task_id);t?(t.status=o.status,t.progress=o.progress,o.result_url&&(t.result_path=o.result_url),o.error&&(t.error=o.error)):K(),o.status==="done"&&(u.success(t("mediaView.genDone")),Q()),o.status==="failed"&&o.error&&u.error(`${t("mediaView.genFailed")}：${o.error}`)}async function j(){if(!y.value.trim())return;const o=p.value.find(e=>e.name===n.value),t=o==null?void 0:o.voice_ref;if(!t){u.error(s("mediaView.noVoiceForAgent"));return}$.value=!0;try{const e=await ge("/media/tts",{text:y.value,voice:t,style:l.value||""});k.value=e.audio_url,e.cached&&u.info(s("mediaView.cacheHit")),Q()}catch(e){u.error(e.message)}finally{$.value=!1}}async function Y(o){try{await _.setAutoSpeak(o),u.success(`自动朗读已${o?s("mediaView.autoSpeakOn"):s("mediaView.autoSpeakOff")} ✓`)}catch(t){u.error(t.message)}}async function te(){try{const o=await L("/media/tts/voices");g.value=o.groups||{},x.value=o.styles.map(t=>({label:t,value:t}))}catch{}}function $e(o){var ue;const e=(ue=o.target.files)==null?void 0:ue[0];if(!e||!h.value)return;const A=e.name.replace(/\.[^.]+$/,"");S.value=!0;const re=new FormData;re.append("name",A),re.append("file",e),Ge.uploadVoiceRef(h.value,re).then(async()=>{u.success(s("mediaView.voiceUploaded")),v.value&&(v.value.value=""),await te(),await Ce()}).catch(Re=>{u.error(Re.message)}).finally(()=>{S.value=!1})}async function Ce(){try{const o=await L("/agents");p.value=o.map(t=>({name:t.name,display_name:t.display_name||t.name,voice_ref:t.voice_ref}))}catch{}}const M=I(()=>p.value.find(o=>o.name===h.value)),ae=I(()=>g.value[h.value]||[]);async function Se(o){if(h.value)try{await Ue(`/agents/${h.value}`,{voice_ref:o});const t=p.value.find(e=>e.name===h.value);t&&(t.voice_ref=o),u.success(s("mediaView.voiceSet"))}catch(t){u.error(t.message)}}async function Ve(o){if(h.value)try{await le(`/media/tts/voices/${h.value}/${o}`),u.success(s("mediaView.voiceDeleted")),await te()}catch(t){u.error(t.message)}}const ce=I(()=>p.value.map(o=>({label:o.display_name,value:o.name})));async function de(o){const t=o==="image"?c.value:q.value;if(t.trim()){D.value=o;try{await ge(`/media/${o}`,{prompt:t}),u.success(s("mediaView.taskQueued")),K()}catch(e){u.error(e.message)}finally{D.value=""}}}async function K(){try{V.value=await L("/media/tasks?limit=20")}catch{}}async function Pe(o){try{await le(`/media/tasks/${o}`),u.success(s("mediaView.cancelled")),K()}catch(t){u.error(t.message)}}async function Q(){try{R.value=await L(`/media/gallery?type=${B.value}&limit=48`)}catch(o){u.error(o.message)}}async function ze(o){try{await le(`/media/gallery/${B.value}/${o}`,!0),R.value=R.value.filter(t=>t.name!==o),u.success(s("mediaView.deleted"))}catch(t){u.error(t.message)}}function Ne(o){window.open(o,"_blank")}const Be={queued:"default",running:"info",done:"success",failed:"error"};return(o,t)=>(C(),N("div",tt,[d("h2",at,"🎙 "+f(a(s)("mediaView.title")),1),m(a(fe),{type:"line",animated:""},{default:b(()=>[m(a(X),{name:"tts",tab:a(s)("mediaView.tts")},{default:b(()=>[d("div",rt,[m(G,{"max-x":4,"max-y":6,style:{flex:"2","min-width":"300px"}},{default:b(()=>[d("div",it,[m(a(ne),{value:y.value,"onUpdate:value":t[0]||(t[0]=e=>y.value=e),type:"textarea",rows:4,placeholder:a(s)("mediaView.ttsInputPh"),maxlength:"500","show-count":""},null,8,["value","placeholder"]),d("div",ot,[m(a(ee),{value:n.value,"onUpdate:value":t[1]||(t[1]=e=>n.value=e),options:ce.value,placeholder:a(s)("mediaView.agentPh"),style:{"max-width":"220px"}},null,8,["value","options","placeholder"]),m(a(ee),{value:l.value,"onUpdate:value":t[2]||(t[2]=e=>l.value=e),options:x.value,placeholder:a(s)("mediaView.emotionPh"),clearable:"",style:{"max-width":"180px"}},null,8,["value","options","placeholder"]),m(a(F),{type:"primary",loading:$.value,onClick:j},{default:b(()=>[T("🎵 "+f(a(s)("mediaView.synthesize")),1)]),_:1},8,["loading"])]),k.value?(C(),N("audio",{key:0,src:k.value,controls:"",autoplay:"",class:"tts-player"},null,8,st)):W("",!0)])]),_:1}),m(G,{"max-x":4,"max-y":6,style:{flex:"1","min-width":"220px"}},{default:b(()=>[d("div",lt,[d("h4",null,f(a(s)("mediaView.readSettings")),1),d("label",nt,[T(f(a(s)("mediaView.autoSpeak"))+" ",1),m(a(He),{value:a(_).autoSpeak,"onUpdate:value":Y},null,8,["value"])]),d("p",ct,f(a(s)("mediaView.autoSpeakDesc")),1)])]),_:1})]),m(G,{"max-x":4,"max-y":6},{default:b(()=>[d("div",dt,[d("h4",null,f(a(s)("mediaView.voiceManage")),1),d("p",ut,f(a(s)("mediaView.voiceUploadHint")),1),d("div",pt,[m(a(ee),{value:h.value,"onUpdate:value":t[3]||(t[3]=e=>h.value=e),options:ce.value,placeholder:a(s)("mediaView.voiceManage"),style:{"max-width":"200px"}},null,8,["value","options","placeholder"])]),M.value?(C(),N("div",gt,[d("div",ft,[d("span",mt,f(M.value.display_name),1),m(a(me),{size:"tiny",bordered:!1},{default:b(()=>[T(f(a(Z)(M.value.name)),1)]),_:1}),d("span",vt,f(a(Z)(M.value.voice_ref?M.value.voice_ref.split("/").pop():a(s)("mediaView.noVoice"))),1)]),d("div",ht,[d("div",yt,[d("input",{ref_key:"voiceInputEl",ref:v,type:"file",accept:"audio/mpeg,audio/wav",style:{display:"none"},onChange:$e},null,544),m(a(F),{size:"small",loading:S.value,onClick:t[4]||(t[4]=e=>{var A;return(A=v.value)==null?void 0:A.click()})},{default:b(()=>[T(" 📁 "+f(a(s)("mediaView.selectAudio")),1)]),_:1},8,["loading"])]),d("div",bt,[m(a(ee),{value:M.value.voice_ref,options:ae.value.map(e=>({label:a(Z)(e.name),value:e.voice_ref})),placeholder:a(s)("mediaView.noVoice"),size:"small",clearable:"",style:{"max-width":"240px"},"onUpdate:value":t[5]||(t[5]=e=>Se(e))},null,8,["value","options","placeholder"])]),d("div",wt,[(C(!0),N(ie,null,oe(ae.value,e=>(C(),N("div",{key:e.voice_ref,class:"voice-item"},[d("span",{class:Me(["voice-name",{active:M.value.voice_ref===e.voice_ref}])},f(a(Z)(e.name)),3),m(a(ve),{onPositiveClick:A=>Ve(e.name)},{trigger:b(()=>[m(a(F),{size:"tiny",type:"error",quaternary:""},{default:b(()=>[...t[11]||(t[11]=[T("🗑",-1)])]),_:1})]),default:b(()=>[T(" "+f(a(s)("mediaView.voiceDeleteConfirm")),1)]),_:1},8,["onPositiveClick"])]))),128)),ae.value.length?W("",!0):(C(),N("div",xt,f(a(s)("mediaView.noVoices")),1))])])])):W("",!0)])]),_:1})]),_:1},8,["tab"]),m(a(X),{name:"image",tab:a(s)("mediaView.imageGen")},{default:b(()=>[m(G,{"max-x":4,"max-y":6},{default:b(()=>[d("div",_t,[m(a(ne),{value:c.value,"onUpdate:value":t[6]||(t[6]=e=>c.value=e),type:"textarea",rows:3,placeholder:a(s)("mediaView.imagePromptPh")},null,8,["value","placeholder"]),m(a(F),{type:"primary",style:{"margin-top":"10px"},loading:D.value==="image",onClick:t[7]||(t[7]=e=>de("image"))},{default:b(()=>[T(" 🎨 "+f(a(s)("mediaView.submit")),1)]),_:1},8,["loading"])])]),_:1})]),_:1},8,["tab"]),m(a(X),{name:"video",tab:a(s)("mediaView.videoGen")},{default:b(()=>[m(G,{"max-x":4,"max-y":6},{default:b(()=>[d("div",kt,[d("p",$t,f(a(s)("mediaView.videoHint"))+" 当前队列 "+f(V.value.filter(e=>e.status==="queued"||e.status==="running").length)+" "+f(a(s)("mediaView.queueCount"))+"。",1),m(a(ne),{value:q.value,"onUpdate:value":t[8]||(t[8]=e=>q.value=e),type:"textarea",rows:3,placeholder:a(s)("mediaView.videoPromptPh")},null,8,["value","placeholder"]),m(a(F),{type:"primary",style:{"margin-top":"10px"},loading:D.value==="video",onClick:t[9]||(t[9]=e=>de("video"))},{default:b(()=>[T(" 🎬 "+f(a(s)("mediaView.submit")),1)]),_:1},8,["loading"])])]),_:1})]),_:1},8,["tab"])]),_:1}),m(G,{"max-x":4,"max-y":6},{default:b(()=>[d("section",Ct,[d("h3",null,f(a(s)("mediaView.taskQueue")),1),d("div",St,[(C(!0),N(ie,null,oe(V.value,e=>(C(),N("div",{key:e.id,class:"task-row"},[m(a(me),{size:"small",type:Be[e.status],bordered:!1},{default:b(()=>[T(f(e.status),1)]),_:2},1032,["type"]),d("span",Vt,f(e.kind),1),d("span",Pt,f(e.prompt),1),e.status==="running"?(C(),se(a(et),{key:0,type:"line",percentage:Math.round((e.progress||0)*100),style:{"max-width":"140px"},height:6},null,8,["percentage"])):W("",!0),e.error?(C(),N("span",zt,f(e.error),1)):W("",!0),e.result_path&&e.status==="done"?(C(),N("a",{key:2,href:e.result_path,target:"_blank",class:"task-link"},f(a(s)("mediaView.view")),9,Nt)):W("",!0),e.status==="queued"?(C(),se(a(F),{key:3,size:"tiny",quaternary:"",onClick:A=>Pe(e.id)},{default:b(()=>[T(f(a(s)("cancel")),1)]),_:1},8,["onClick"])):W("",!0)]))),128)),V.value.length?W("",!0):(C(),N("div",Bt,f(a(s)("mediaView.queueEmpty")),1))])])]),_:1}),d("section",Rt,[d("div",Dt,[d("h3",null,f(a(s)("mediaView.gallery")),1),m(a(fe),{type:"segment",size:"small",value:B.value,"onUpdate:value":[t[10]||(t[10]=e=>B.value=e),Q],style:{"max-width":"280px"}},{default:b(()=>[m(a(X),{name:"image",tab:a(s)("mediaView.image")},null,8,["tab"]),m(a(X),{name:"video",tab:a(s)("mediaView.video")},null,8,["tab"]),m(a(X),{name:"audio",tab:a(s)("mediaView.audio")},null,8,["tab"])]),_:1},8,["value"])]),d("div",It,[(C(!0),N(ie,null,oe(R.value,e=>(C(),se(G,{key:e.name},{default:b(()=>[d("div",qt,[B.value==="image"?(C(),N("img",{key:0,src:e.url,loading:"lazy",onClick:A=>Ne(e.url)},null,8,Tt)):B.value==="video"?(C(),N("video",{key:1,src:e.url,controls:"",preload:"metadata"},null,8,Wt)):(C(),N("audio",{key:2,src:e.url,controls:""},null,8,At)),d("div",Ot,[d("span",jt,f(e.name),1),m(a(ve),{onPositiveClick:A=>ze(e.name)},{trigger:b(()=>[...t[12]||(t[12]=[d("button",{class:"g-del"},"🗑",-1)])]),default:b(()=>[T(" "+f(a(s)("mediaView.deleteConfirm")),1)]),_:1},8,["onPositiveClick"])])])]),_:2},1024))),128)),R.value.length?W("",!0):(C(),N("div",Mt,f(a(s)("mediaView.emptyGallery")),1))])])]))}}),ra=Le(Gt,[["__scopeId","data-v-41754cfb"]]);export{ra as default};

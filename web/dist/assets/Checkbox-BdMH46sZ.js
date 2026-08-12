import{d as H,ay as d,aG as j,aQ as E,r as _,f as F,aK as be,aX as ue,aW as P,aU as l,aC as f,az as a,aA as $,aB as R,bO as he,a_ as fe,a$ as ve,aE as ke,bQ as me,bI as xe,aF as G,aH as ge,aI as pe,bH as Ce,at as ye,a3 as we,ce as ze,aJ as K}from"./index-BzbiSk6Z.js";import{c as O}from"./Suffix-ComXbo0C.js";const V=be("n-checkbox-group"),Re={min:Number,max:Number,size:String,value:Array,defaultValue:{type:Array,default:null},disabled:{type:Boolean,default:void 0},"onUpdate:value":[Function,Array],onUpdateValue:[Function,Array],onChange:[Function,Array]},Be=H({name:"CheckboxGroup",props:Re,setup(o){const{mergedClsPrefixRef:i}=j(o),g=E(o),{mergedSizeRef:S,mergedDisabledRef:T}=g,p=_(o.defaultValue),D=F(()=>o.value),b=O(D,p),I=F(()=>{var s;return((s=b.value)===null||s===void 0?void 0:s.length)||0}),n=F(()=>Array.isArray(b.value)?new Set(b.value):new Set);function C(s,r){const{nTriggerFormInput:y,nTriggerFormChange:v}=g,{onChange:c,"onUpdate:value":k,onUpdateValue:m}=o;if(Array.isArray(b.value)){const t=Array.from(b.value),M=t.findIndex(U=>U===r);s?~M||(t.push(r),m&&l(m,t,{actionType:"check",value:r}),k&&l(k,t,{actionType:"check",value:r}),y(),v(),p.value=t,c&&l(c,t)):~M&&(t.splice(M,1),m&&l(m,t,{actionType:"uncheck",value:r}),k&&l(k,t,{actionType:"uncheck",value:r}),c&&l(c,t),p.value=t,y(),v())}else s?(m&&l(m,[r],{actionType:"check",value:r}),k&&l(k,[r],{actionType:"check",value:r}),c&&l(c,[r]),p.value=[r],y(),v()):(m&&l(m,[],{actionType:"uncheck",value:r}),k&&l(k,[],{actionType:"uncheck",value:r}),c&&l(c,[]),p.value=[],y(),v())}return ue(V,{checkedCountRef:I,maxRef:P(o,"max"),minRef:P(o,"min"),valueSetRef:n,disabledRef:T,mergedSizeRef:S,toggleCheckbox:C}),{mergedClsPrefix:i}},render(){return d("div",{class:`${this.mergedClsPrefix}-checkbox-group`,role:"group"},this.$slots)}}),Se=()=>d("svg",{viewBox:"0 0 64 64",class:"check-icon"},d("path",{d:"M50.42,16.76L22.34,39.45l-8.1-11.46c-1.12-1.58-3.3-1.96-4.88-0.84c-1.58,1.12-1.95,3.3-0.84,4.88l10.26,14.51  c0.56,0.79,1.42,1.31,2.38,1.45c0.16,0.02,0.32,0.03,0.48,0.03c0.8,0,1.57-0.27,2.2-0.78l30.99-25.03c1.5-1.21,1.74-3.42,0.52-4.92  C54.13,15.78,51.93,15.55,50.42,16.76z"})),Te=()=>d("svg",{viewBox:"0 0 100 100",class:"line-icon"},d("path",{d:"M80.2,55.5H21.4c-2.8,0-5.1-2.5-5.1-5.5l0,0c0-3,2.3-5.5,5.1-5.5h58.7c2.8,0,5.1,2.5,5.1,5.5l0,0C85.2,53.1,82.9,55.5,80.2,55.5z"})),De=f([a("checkbox",`
 font-size: var(--n-font-size);
 outline: none;
 cursor: pointer;
 display: inline-flex;
 flex-wrap: nowrap;
 align-items: flex-start;
 word-break: break-word;
 line-height: var(--n-size);
 --n-merged-color-table: var(--n-color-table);
 `,[$("show-label","line-height: var(--n-label-line-height);"),f("&:hover",[a("checkbox-box",[R("border","border: var(--n-border-checked);")])]),f("&:focus:not(:active)",[a("checkbox-box",[R("border",`
 border: var(--n-border-focus);
 box-shadow: var(--n-box-shadow-focus);
 `)])]),$("inside-table",[a("checkbox-box",`
 background-color: var(--n-merged-color-table);
 `)]),$("checked",[a("checkbox-box",`
 background-color: var(--n-color-checked);
 `,[a("checkbox-icon",[f(".check-icon",`
 opacity: 1;
 transform: scale(1);
 `)])])]),$("indeterminate",[a("checkbox-box",[a("checkbox-icon",[f(".check-icon",`
 opacity: 0;
 transform: scale(.5);
 `),f(".line-icon",`
 opacity: 1;
 transform: scale(1);
 `)])])]),$("checked, indeterminate",[f("&:focus:not(:active)",[a("checkbox-box",[R("border",`
 border: var(--n-border-checked);
 box-shadow: var(--n-box-shadow-focus);
 `)])]),a("checkbox-box",`
 background-color: var(--n-color-checked);
 border-left: 0;
 border-top: 0;
 `,[R("border",{border:"var(--n-border-checked)"})])]),$("disabled",{cursor:"not-allowed"},[$("checked",[a("checkbox-box",`
 background-color: var(--n-color-disabled-checked);
 `,[R("border",{border:"var(--n-border-disabled-checked)"}),a("checkbox-icon",[f(".check-icon, .line-icon",{fill:"var(--n-check-mark-color-disabled-checked)"})])])]),a("checkbox-box",`
 background-color: var(--n-color-disabled);
 `,[R("border",`
 border: var(--n-border-disabled);
 `),a("checkbox-icon",[f(".check-icon, .line-icon",`
 fill: var(--n-check-mark-color-disabled);
 `)])]),R("label",`
 color: var(--n-text-color-disabled);
 `)]),a("checkbox-box-wrapper",`
 position: relative;
 width: var(--n-size);
 flex-shrink: 0;
 flex-grow: 0;
 user-select: none;
 -webkit-user-select: none;
 `),a("checkbox-box",`
 position: absolute;
 left: 0;
 top: 50%;
 transform: translateY(-50%);
 height: var(--n-size);
 width: var(--n-size);
 display: inline-block;
 box-sizing: border-box;
 border-radius: var(--n-border-radius);
 background-color: var(--n-color);
 transition: background-color 0.3s var(--n-bezier);
 `,[R("border",`
 transition:
 border-color .3s var(--n-bezier),
 box-shadow .3s var(--n-bezier);
 border-radius: inherit;
 position: absolute;
 left: 0;
 right: 0;
 top: 0;
 bottom: 0;
 border: var(--n-border);
 `),a("checkbox-icon",`
 display: flex;
 align-items: center;
 justify-content: center;
 position: absolute;
 left: 1px;
 right: 1px;
 top: 1px;
 bottom: 1px;
 `,[f(".check-icon, .line-icon",`
 width: 100%;
 fill: var(--n-check-mark-color);
 opacity: 0;
 transform: scale(0.5);
 transform-origin: center;
 transition:
 fill 0.3s var(--n-bezier),
 transform 0.3s var(--n-bezier),
 opacity 0.3s var(--n-bezier),
 border-color 0.3s var(--n-bezier);
 `),he({left:"1px",top:"1px"})])]),R("label",`
 color: var(--n-text-color);
 transition: color .3s var(--n-bezier);
 user-select: none;
 -webkit-user-select: none;
 padding: var(--n-label-padding);
 font-weight: var(--n-label-font-weight);
 `,[f("&:empty",{display:"none"})])]),fe(a("checkbox",`
 --n-merged-color-table: var(--n-color-table-modal);
 `)),ve(a("checkbox",`
 --n-merged-color-table: var(--n-color-table-popover);
 `))]),$e=Object.assign(Object.assign({},G.props),{size:String,checked:{type:[Boolean,String,Number],default:void 0},defaultChecked:{type:[Boolean,String,Number],default:!1},value:[String,Number],disabled:{type:Boolean,default:void 0},indeterminate:Boolean,label:String,focusable:{type:Boolean,default:!0},checkedValue:{type:[Boolean,String,Number],default:!0},uncheckedValue:{type:[Boolean,String,Number],default:!1},"onUpdate:checked":[Function,Array],onUpdateChecked:[Function,Array],privateInsideTable:Boolean,onChange:[Function,Array]}),Me=H({name:"Checkbox",props:$e,setup(o){const i=we(V,null),g=_(null),{mergedClsPrefixRef:S,inlineThemeDisabled:T,mergedRtlRef:p,mergedComponentPropsRef:D}=j(o),b=_(o.defaultChecked),I=P(o,"checked"),n=O(I,b),C=xe(()=>{if(i){const e=i.valueSetRef.value;return e&&o.value!==void 0?e.has(o.value):!1}else return n.value===o.checkedValue}),s=E(o,{mergedSize(e){var u,h;const{size:x}=o;if(x!==void 0)return x;if(i){const{value:z}=i.mergedSizeRef;if(z!==void 0)return z}if(e){const{mergedSize:z}=e;if(z!==void 0)return z.value}const w=(h=(u=D==null?void 0:D.value)===null||u===void 0?void 0:u.Checkbox)===null||h===void 0?void 0:h.size;return w||"medium"},mergedDisabled(e){const{disabled:u}=o;if(u!==void 0)return u;if(i){if(i.disabledRef.value)return!0;const{maxRef:{value:h},checkedCountRef:x}=i;if(h!==void 0&&x.value>=h&&!C.value)return!0;const{minRef:{value:w}}=i;if(w!==void 0&&x.value<=w&&C.value)return!0}return e?e.disabled.value:!1}}),{mergedDisabledRef:r,mergedSizeRef:y}=s,v=G("Checkbox","-checkbox",De,ze,o,S);function c(e){if(i&&o.value!==void 0)i.toggleCheckbox(!C.value,o.value);else{const{onChange:u,"onUpdate:checked":h,onUpdateChecked:x}=o,{nTriggerFormInput:w,nTriggerFormChange:z}=s,B=C.value?o.uncheckedValue:o.checkedValue;h&&l(h,B,e),x&&l(x,B,e),u&&l(u,B,e),w(),z(),b.value=B}}function k(e){r.value||c(e)}function m(e){if(!r.value)switch(e.key){case" ":case"Enter":c(e)}}function t(e){switch(e.key){case" ":e.preventDefault()}}const M={focus:()=>{var e;(e=g.value)===null||e===void 0||e.focus()},blur:()=>{var e;(e=g.value)===null||e===void 0||e.blur()}},U=ge("Checkbox",p,S),N=F(()=>{const{value:e}=y,{common:{cubicBezierEaseInOut:u},self:{borderRadius:h,color:x,colorChecked:w,colorDisabled:z,colorTableHeader:B,colorTableHeaderModal:L,colorTableHeaderPopover:W,checkMarkColor:Q,checkMarkColorDisabled:J,border:X,borderFocus:Y,borderDisabled:q,borderChecked:Z,boxShadowFocus:ee,textColor:oe,textColorDisabled:re,checkMarkColorDisabledChecked:ae,colorDisabledChecked:ne,borderDisabledChecked:ce,labelPadding:le,labelLineHeight:ie,labelFontWeight:te,[K("fontSize",e)]:de,[K("size",e)]:se}}=v.value;return{"--n-label-line-height":ie,"--n-label-font-weight":te,"--n-size":se,"--n-bezier":u,"--n-border-radius":h,"--n-border":X,"--n-border-checked":Z,"--n-border-focus":Y,"--n-border-disabled":q,"--n-border-disabled-checked":ce,"--n-box-shadow-focus":ee,"--n-color":x,"--n-color-checked":w,"--n-color-table":B,"--n-color-table-modal":L,"--n-color-table-popover":W,"--n-color-disabled":z,"--n-color-disabled-checked":ne,"--n-text-color":oe,"--n-text-color-disabled":re,"--n-check-mark-color":Q,"--n-check-mark-color-disabled":J,"--n-check-mark-color-disabled-checked":ae,"--n-font-size":de,"--n-label-padding":le}}),A=T?pe("checkbox",F(()=>y.value[0]),N,o):void 0;return Object.assign(s,M,{rtlEnabled:U,selfRef:g,mergedClsPrefix:S,mergedDisabled:r,renderedChecked:C,mergedTheme:v,labelId:Ce(),handleClick:k,handleKeyUp:m,handleKeyDown:t,cssVars:T?void 0:N,themeClass:A==null?void 0:A.themeClass,onRender:A==null?void 0:A.onRender})},render(){var o;const{$slots:i,renderedChecked:g,mergedDisabled:S,indeterminate:T,privateInsideTable:p,cssVars:D,labelId:b,label:I,mergedClsPrefix:n,focusable:C,handleKeyUp:s,handleKeyDown:r,handleClick:y}=this;(o=this.onRender)===null||o===void 0||o.call(this);const v=ke(i.default,c=>I||c?d("span",{class:`${n}-checkbox__label`,id:b},I||c):null);return d("div",{ref:"selfRef",class:[`${n}-checkbox`,this.themeClass,this.rtlEnabled&&`${n}-checkbox--rtl`,g&&`${n}-checkbox--checked`,S&&`${n}-checkbox--disabled`,T&&`${n}-checkbox--indeterminate`,p&&`${n}-checkbox--inside-table`,v&&`${n}-checkbox--show-label`],tabindex:S||!C?void 0:0,role:"checkbox","aria-checked":T?"mixed":g,"aria-labelledby":b,style:D,onKeyup:s,onKeydown:r,onClick:y,onMousedown:()=>{ye("selectstart",window,c=>{c.preventDefault()},{once:!0})}},d("div",{class:`${n}-checkbox-box-wrapper`}," ",d("div",{class:`${n}-checkbox-box`},d(me,null,{default:()=>this.indeterminate?d("div",{key:"indeterminate",class:`${n}-checkbox-icon`},Te()):d("div",{key:"check",class:`${n}-checkbox-icon`},Se())}),d("div",{class:`${n}-checkbox-box__border`}))),v)}});export{Be as N,Me as a};

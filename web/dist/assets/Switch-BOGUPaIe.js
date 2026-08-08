import{bm as de,eh as ce,dN as ue,a9 as H,ab as i,dU as I,a7 as D,aa as l,b3 as L,d as he,ei as W,R as a,dy as y,Y as be,af as E,b5 as fe,r as K,ah as ve,dX as ge,ec as we,f as F,ak as M,bh as x,ea as U,dI as s,am as me}from"./index-Cdia8NDl.js";import{u as pe}from"./Suffix-BhyfFU_7.js";function ye(e){const{primaryColor:d,opacityDisabled:f,borderRadius:n,textColor3:v}=e;return Object.assign(Object.assign({},ce),{iconColor:v,textColor:"white",loadingColor:d,opacityDisabled:f,railColor:"rgba(0, 0, 0, .14)",railColorActive:d,buttonBoxShadow:"0 1px 4px 0 rgba(0, 0, 0, 0.3), inset 0 0 1px 0 rgba(0, 0, 0, 0.05)",buttonColor:"#FFF",railBorderRadiusSmall:n,railBorderRadiusMedium:n,railBorderRadiusLarge:n,buttonBorderRadiusSmall:n,buttonBorderRadiusMedium:n,buttonBorderRadiusLarge:n,boxShadowFocus:`0 0 0 2px ${ue(d,{alpha:.2})}`})}const xe={common:de,self:ye},ke=H("switch",`
 height: var(--n-height);
 min-width: var(--n-width);
 vertical-align: middle;
 user-select: none;
 -webkit-user-select: none;
 display: inline-flex;
 outline: none;
 justify-content: center;
 align-items: center;
`,[i("children-placeholder",`
 height: var(--n-rail-height);
 display: flex;
 flex-direction: column;
 overflow: hidden;
 pointer-events: none;
 visibility: hidden;
 `),i("rail-placeholder",`
 display: flex;
 flex-wrap: none;
 `),i("button-placeholder",`
 width: calc(1.75 * var(--n-rail-height));
 height: var(--n-rail-height);
 `),H("base-loading",`
 position: absolute;
 top: 50%;
 left: 50%;
 transform: translateX(-50%) translateY(-50%);
 font-size: calc(var(--n-button-width) - 4px);
 color: var(--n-loading-color);
 transition: color .3s var(--n-bezier);
 `,[I({left:"50%",top:"50%",originalTransform:"translateX(-50%) translateY(-50%)"})]),i("checked, unchecked",`
 transition: color .3s var(--n-bezier);
 color: var(--n-text-color);
 box-sizing: border-box;
 position: absolute;
 white-space: nowrap;
 top: 0;
 bottom: 0;
 display: flex;
 align-items: center;
 line-height: 1;
 `),i("checked",`
 right: 0;
 padding-right: calc(1.25 * var(--n-rail-height) - var(--n-offset));
 `),i("unchecked",`
 left: 0;
 justify-content: flex-end;
 padding-left: calc(1.25 * var(--n-rail-height) - var(--n-offset));
 `),D("&:focus",[i("rail",`
 box-shadow: var(--n-box-shadow-focus);
 `)]),l("round",[i("rail","border-radius: calc(var(--n-rail-height) / 2);",[i("button","border-radius: calc(var(--n-button-height) / 2);")])]),L("disabled",[L("icon",[l("rubber-band",[l("pressed",[i("rail",[i("button","max-width: var(--n-button-width-pressed);")])]),i("rail",[D("&:active",[i("button","max-width: var(--n-button-width-pressed);")])]),l("active",[l("pressed",[i("rail",[i("button","left: calc(100% - var(--n-offset) - var(--n-button-width-pressed));")])]),i("rail",[D("&:active",[i("button","left: calc(100% - var(--n-offset) - var(--n-button-width-pressed));")])])])])])]),l("active",[i("rail",[i("button","left: calc(100% - var(--n-button-width) - var(--n-offset))")])]),i("rail",`
 overflow: hidden;
 height: var(--n-rail-height);
 min-width: var(--n-rail-width);
 border-radius: var(--n-rail-border-radius);
 cursor: pointer;
 position: relative;
 transition:
 opacity .3s var(--n-bezier),
 background .3s var(--n-bezier),
 box-shadow .3s var(--n-bezier);
 background-color: var(--n-rail-color);
 `,[i("button-icon",`
 color: var(--n-icon-color);
 transition: color .3s var(--n-bezier);
 font-size: calc(var(--n-button-height) - 4px);
 position: absolute;
 left: 0;
 right: 0;
 top: 0;
 bottom: 0;
 display: flex;
 justify-content: center;
 align-items: center;
 line-height: 1;
 `,[I()]),i("button",`
 align-items: center; 
 top: var(--n-offset);
 left: var(--n-offset);
 height: var(--n-button-height);
 width: var(--n-button-width-pressed);
 max-width: var(--n-button-width);
 border-radius: var(--n-button-border-radius);
 background-color: var(--n-button-color);
 box-shadow: var(--n-button-box-shadow);
 box-sizing: border-box;
 cursor: inherit;
 content: "";
 position: absolute;
 transition:
 background-color .3s var(--n-bezier),
 left .3s var(--n-bezier),
 opacity .3s var(--n-bezier),
 max-width .3s var(--n-bezier),
 box-shadow .3s var(--n-bezier);
 `)]),l("active",[i("rail","background-color: var(--n-rail-color-active);")]),l("loading",[i("rail",`
 cursor: wait;
 `)]),l("disabled",[i("rail",`
 cursor: not-allowed;
 opacity: .5;
 `)])]),Se=Object.assign(Object.assign({},E.props),{size:String,value:{type:[String,Number,Boolean],default:void 0},loading:Boolean,defaultValue:{type:[String,Number,Boolean],default:!1},disabled:{type:Boolean,default:void 0},round:{type:Boolean,default:!0},"onUpdate:value":[Function,Array],onUpdateValue:[Function,Array],checkedValue:{type:[String,Number,Boolean],default:!0},uncheckedValue:{type:[String,Number,Boolean],default:!1},railStyle:Function,rubberBand:{type:Boolean,default:!0},spinProps:Object,onChange:[Function,Array]});let $;const Be=he({name:"Switch",props:Se,slots:Object,setup(e){$===void 0&&(typeof CSS<"u"?typeof CSS.supports<"u"?$=CSS.supports("width","max(1px)"):$=!1:$=!0);const{mergedClsPrefixRef:d,inlineThemeDisabled:f,mergedComponentPropsRef:n}=be(e),v=E("Switch","-switch",ke,xe,e,d),g=fe(e,{mergedSize(t){var c,u;if(e.size!==void 0)return e.size;if(t)return t.mergedSize.value;const p=(u=(c=n==null?void 0:n.value)===null||c===void 0?void 0:c.Switch)===null||u===void 0?void 0:u.size;return p||"medium"}}),{mergedSizeRef:S,mergedDisabledRef:w}=g,C=K(e.defaultValue),z=me(e,"value"),m=pe(z,C),_=F(()=>m.value===e.checkedValue),o=K(!1),r=K(!1),R=F(()=>{const{railStyle:t}=e;if(t)return t({focused:r.value,checked:_.value})});function V(t){const{"onUpdate:value":c,onChange:u,onUpdateValue:p}=e,{nTriggerFormInput:j,nTriggerFormChange:O}=g;c&&M(c,t),p&&M(p,t),u&&M(u,t),C.value=t,j(),O()}function X(){const{nTriggerFormFocus:t}=g;t()}function Y(){const{nTriggerFormBlur:t}=g;t()}function q(){e.loading||w.value||(m.value!==e.checkedValue?V(e.checkedValue):V(e.uncheckedValue))}function G(){r.value=!0,X()}function J(){r.value=!1,Y(),o.value=!1}function Q(t){e.loading||w.value||t.key===" "&&(m.value!==e.checkedValue?V(e.checkedValue):V(e.uncheckedValue),o.value=!1)}function Z(t){e.loading||w.value||t.key===" "&&(t.preventDefault(),o.value=!0)}const A=F(()=>{const{value:t}=S,{self:{opacityDisabled:c,railColor:u,railColorActive:p,buttonBoxShadow:j,buttonColor:O,boxShadowFocus:ee,loadingColor:te,textColor:ie,iconColor:oe,[x("buttonHeight",t)]:h,[x("buttonWidth",t)]:ae,[x("buttonWidthPressed",t)]:ne,[x("railHeight",t)]:b,[x("railWidth",t)]:B,[x("railBorderRadius",t)]:re,[x("buttonBorderRadius",t)]:le},common:{cubicBezierEaseInOut:se}}=v.value;let P,T,N;return $?(P=`calc((${b} - ${h}) / 2)`,T=`max(${b}, ${h})`,N=`max(${B}, calc(${B} + ${h} - ${b}))`):(P=U((s(b)-s(h))/2),T=U(Math.max(s(b),s(h))),N=s(b)>s(h)?B:U(s(B)+s(h)-s(b))),{"--n-bezier":se,"--n-button-border-radius":le,"--n-button-box-shadow":j,"--n-button-color":O,"--n-button-width":ae,"--n-button-width-pressed":ne,"--n-button-height":h,"--n-height":T,"--n-offset":P,"--n-opacity-disabled":c,"--n-rail-border-radius":re,"--n-rail-color":u,"--n-rail-color-active":p,"--n-rail-height":b,"--n-rail-width":B,"--n-width":N,"--n-box-shadow-focus":ee,"--n-loading-color":te,"--n-text-color":ie,"--n-icon-color":oe}}),k=f?ve("switch",F(()=>S.value[0]),A,e):void 0;return{handleClick:q,handleBlur:J,handleFocus:G,handleKeyup:Q,handleKeydown:Z,mergedRailStyle:R,pressed:o,mergedClsPrefix:d,mergedValue:m,checked:_,mergedDisabled:w,cssVars:f?void 0:A,themeClass:k==null?void 0:k.themeClass,onRender:k==null?void 0:k.onRender}},render(){const{mergedClsPrefix:e,mergedDisabled:d,checked:f,mergedRailStyle:n,onRender:v,$slots:g}=this;v==null||v();const{checked:S,unchecked:w,icon:C,"checked-icon":z,"unchecked-icon":m}=g,_=!(W(C)&&W(z)&&W(m));return a("div",{role:"switch","aria-checked":f,class:[`${e}-switch`,this.themeClass,_&&`${e}-switch--icon`,f&&`${e}-switch--active`,d&&`${e}-switch--disabled`,this.round&&`${e}-switch--round`,this.loading&&`${e}-switch--loading`,this.pressed&&`${e}-switch--pressed`,this.rubberBand&&`${e}-switch--rubber-band`],tabindex:this.mergedDisabled?void 0:0,style:this.cssVars,onClick:this.handleClick,onFocus:this.handleFocus,onBlur:this.handleBlur,onKeyup:this.handleKeyup,onKeydown:this.handleKeydown},a("div",{class:`${e}-switch__rail`,"aria-hidden":"true",style:n},y(S,o=>y(w,r=>o||r?a("div",{"aria-hidden":!0,class:`${e}-switch__children-placeholder`},a("div",{class:`${e}-switch__rail-placeholder`},a("div",{class:`${e}-switch__button-placeholder`}),o),a("div",{class:`${e}-switch__rail-placeholder`},a("div",{class:`${e}-switch__button-placeholder`}),r)):null)),a("div",{class:`${e}-switch__button`},y(C,o=>y(z,r=>y(m,R=>a(ge,null,{default:()=>this.loading?a(we,Object.assign({key:"loading",clsPrefix:e,strokeWidth:20},this.spinProps)):this.checked&&(r||o)?a("div",{class:`${e}-switch__button-icon`,key:r?"checked-icon":"icon"},r||o):!this.checked&&(R||o)?a("div",{class:`${e}-switch__button-icon`,key:R?"unchecked-icon":"icon"},R||o):null})))),y(S,o=>o&&a("div",{key:"checked",class:`${e}-switch__checked`},o)),y(w,o=>o&&a("div",{key:"unchecked",class:`${e}-switch__unchecked`},o)))))}});export{Be as N};

import{f as S,bl as so,eP as to,dV as r,a9 as io,aa as u,ab as m,b2 as I,a7 as z,d as ho,dx as U,R as x,an as go,Y as bo,af as D,Z as Co,ah as vo,r as uo,ak as fo,bg as h,dT as po,eQ as V,a3 as ko,am as mo,b0 as xo}from"./index-ndYglh2w.js";function Ho(l,t){return S(()=>{for(const e of t)if(l[e]!==void 0)return l[e];return l[t[t.length-1]]})}function Po(l){const{textColor2:t,primaryColorHover:e,primaryColorPressed:f,primaryColor:c,infoColor:d,successColor:n,warningColor:s,errorColor:i,baseColor:p,borderColor:k,opacityDisabled:b,tagColor:B,closeIconColor:P,closeIconColorHover:v,closeIconColorPressed:o,borderRadiusSmall:a,fontSizeMini:C,fontSizeTiny:g,fontSizeSmall:$,fontSizeMedium:H,heightMini:R,heightTiny:T,heightSmall:M,heightMedium:_,closeColorHover:E,closeColorPressed:O,buttonColor2Hover:W,buttonColor2Pressed:j,fontWeightStrong:w}=l;return Object.assign(Object.assign({},to),{closeBorderRadius:a,heightTiny:R,heightSmall:T,heightMedium:M,heightLarge:_,borderRadius:a,opacityDisabled:b,fontSizeTiny:C,fontSizeSmall:g,fontSizeMedium:$,fontSizeLarge:H,fontWeightStrong:w,textColorCheckable:t,textColorHoverCheckable:t,textColorPressedCheckable:t,textColorChecked:p,colorCheckable:"#0000",colorHoverCheckable:W,colorPressedCheckable:j,colorChecked:c,colorCheckedHover:e,colorCheckedPressed:f,border:`1px solid ${k}`,textColor:t,color:B,colorBordered:"rgb(250, 250, 252)",closeIconColor:P,closeIconColorHover:v,closeIconColorPressed:o,closeColorHover:E,closeColorPressed:O,borderPrimary:`1px solid ${r(c,{alpha:.3})}`,textColorPrimary:c,colorPrimary:r(c,{alpha:.12}),colorBorderedPrimary:r(c,{alpha:.1}),closeIconColorPrimary:c,closeIconColorHoverPrimary:c,closeIconColorPressedPrimary:c,closeColorHoverPrimary:r(c,{alpha:.12}),closeColorPressedPrimary:r(c,{alpha:.18}),borderInfo:`1px solid ${r(d,{alpha:.3})}`,textColorInfo:d,colorInfo:r(d,{alpha:.12}),colorBorderedInfo:r(d,{alpha:.1}),closeIconColorInfo:d,closeIconColorHoverInfo:d,closeIconColorPressedInfo:d,closeColorHoverInfo:r(d,{alpha:.12}),closeColorPressedInfo:r(d,{alpha:.18}),borderSuccess:`1px solid ${r(n,{alpha:.3})}`,textColorSuccess:n,colorSuccess:r(n,{alpha:.12}),colorBorderedSuccess:r(n,{alpha:.1}),closeIconColorSuccess:n,closeIconColorHoverSuccess:n,closeIconColorPressedSuccess:n,closeColorHoverSuccess:r(n,{alpha:.12}),closeColorPressedSuccess:r(n,{alpha:.18}),borderWarning:`1px solid ${r(s,{alpha:.35})}`,textColorWarning:s,colorWarning:r(s,{alpha:.15}),colorBorderedWarning:r(s,{alpha:.12}),closeIconColorWarning:s,closeIconColorHoverWarning:s,closeIconColorPressedWarning:s,closeColorHoverWarning:r(s,{alpha:.12}),closeColorPressedWarning:r(s,{alpha:.18}),borderError:`1px solid ${r(i,{alpha:.23})}`,textColorError:i,colorError:r(i,{alpha:.1}),colorBorderedError:r(i,{alpha:.08}),closeIconColorError:i,closeIconColorHoverError:i,closeIconColorPressedError:i,closeColorHoverError:r(i,{alpha:.12}),closeColorPressedError:r(i,{alpha:.18})})}const yo={name:"Tag",common:so,self:Po},Io={color:Object,type:{type:String,default:"default"},round:Boolean,size:String,closable:Boolean,disabled:{type:Boolean,default:void 0}},zo=io("tag",`
 --n-close-margin: var(--n-close-margin-top) var(--n-close-margin-right) var(--n-close-margin-bottom) var(--n-close-margin-left);
 white-space: nowrap;
 position: relative;
 box-sizing: border-box;
 cursor: default;
 display: inline-flex;
 align-items: center;
 flex-wrap: nowrap;
 padding: var(--n-padding);
 border-radius: var(--n-border-radius);
 color: var(--n-text-color);
 background-color: var(--n-color);
 transition: 
 border-color .3s var(--n-bezier),
 background-color .3s var(--n-bezier),
 color .3s var(--n-bezier),
 box-shadow .3s var(--n-bezier),
 opacity .3s var(--n-bezier);
 line-height: 1;
 height: var(--n-height);
 font-size: var(--n-font-size);
`,[u("strong",`
 font-weight: var(--n-font-weight-strong);
 `),m("border",`
 pointer-events: none;
 position: absolute;
 left: 0;
 right: 0;
 top: 0;
 bottom: 0;
 border-radius: inherit;
 border: var(--n-border);
 transition: border-color .3s var(--n-bezier);
 `),m("icon",`
 display: flex;
 margin: 0 4px 0 0;
 color: var(--n-text-color);
 transition: color .3s var(--n-bezier);
 font-size: var(--n-avatar-size-override);
 `),m("avatar",`
 display: flex;
 margin: 0 6px 0 0;
 `),m("close",`
 margin: var(--n-close-margin);
 transition:
 background-color .3s var(--n-bezier),
 color .3s var(--n-bezier);
 `),u("round",`
 padding: 0 calc(var(--n-height) / 3);
 border-radius: calc(var(--n-height) / 2);
 `,[m("icon",`
 margin: 0 4px 0 calc((var(--n-height) - 8px) / -2);
 `),m("avatar",`
 margin: 0 6px 0 calc((var(--n-height) - 8px) / -2);
 `),u("closable",`
 padding: 0 calc(var(--n-height) / 4) 0 calc(var(--n-height) / 3);
 `)]),u("icon, avatar",[u("round",`
 padding: 0 calc(var(--n-height) / 3) 0 calc(var(--n-height) / 2);
 `)]),u("disabled",`
 cursor: not-allowed !important;
 opacity: var(--n-opacity-disabled);
 `),u("checkable",`
 cursor: pointer;
 box-shadow: none;
 color: var(--n-text-color-checkable);
 background-color: var(--n-color-checkable);
 `,[I("disabled",[z("&:hover","background-color: var(--n-color-hover-checkable);",[I("checked","color: var(--n-text-color-hover-checkable);")]),z("&:active","background-color: var(--n-color-pressed-checkable);",[I("checked","color: var(--n-text-color-pressed-checkable);")])]),u("checked",`
 color: var(--n-text-color-checked);
 background-color: var(--n-color-checked);
 `,[I("disabled",[z("&:hover","background-color: var(--n-color-checked-hover);"),z("&:active","background-color: var(--n-color-checked-pressed);")])])])]),So=Object.assign(Object.assign(Object.assign({},D.props),Io),{bordered:{type:Boolean,default:void 0},checked:Boolean,checkable:Boolean,strong:Boolean,triggerClickOnClose:Boolean,onClose:[Array,Function],onMouseenter:Function,onMouseleave:Function,"onUpdate:checked":Function,onUpdateChecked:Function,internalCloseFocusable:{type:Boolean,default:!0},internalCloseIsButtonTag:{type:Boolean,default:!0},onCheckedChange:Function}),Bo=xo("n-tag"),Ro=ho({name:"Tag",props:So,slots:Object,setup(l){const t=uo(null),{mergedBorderedRef:e,mergedClsPrefixRef:f,inlineThemeDisabled:c,mergedRtlRef:d,mergedComponentPropsRef:n}=bo(l),s=S(()=>{var o,a;return l.size||((a=(o=n==null?void 0:n.value)===null||o===void 0?void 0:o.Tag)===null||a===void 0?void 0:a.size)||"medium"}),i=D("Tag","-tag",zo,yo,l,f);ko(Bo,{roundRef:mo(l,"round")});function p(){if(!l.disabled&&l.checkable){const{checked:o,onCheckedChange:a,onUpdateChecked:C,"onUpdate:checked":g}=l;C&&C(!o),g&&g(!o),a&&a(!o)}}function k(o){if(l.triggerClickOnClose||o.stopPropagation(),!l.disabled){const{onClose:a}=l;a&&fo(a,o)}}const b={setTextContent(o){const{value:a}=t;a&&(a.textContent=o)}},B=Co("Tag",d,f),P=S(()=>{const{type:o,color:{color:a,textColor:C}={}}=l,g=s.value,{common:{cubicBezierEaseInOut:$},self:{padding:H,closeMargin:R,borderRadius:T,opacityDisabled:M,textColorCheckable:_,textColorHoverCheckable:E,textColorPressedCheckable:O,textColorChecked:W,colorCheckable:j,colorHoverCheckable:w,colorPressedCheckable:K,colorChecked:L,colorCheckedHover:A,colorCheckedPressed:Q,closeBorderRadius:Y,fontWeightStrong:Z,[h("colorBordered",o)]:q,[h("closeSize",g)]:G,[h("closeIconSize",g)]:J,[h("fontSize",g)]:X,[h("height",g)]:F,[h("color",o)]:oo,[h("textColor",o)]:eo,[h("border",o)]:ro,[h("closeIconColor",o)]:N,[h("closeIconColorHover",o)]:lo,[h("closeIconColorPressed",o)]:ao,[h("closeColorHover",o)]:co,[h("closeColorPressed",o)]:no}}=i.value,y=po(R);return{"--n-font-weight-strong":Z,"--n-avatar-size-override":`calc(${F} - 8px)`,"--n-bezier":$,"--n-border-radius":T,"--n-border":ro,"--n-close-icon-size":J,"--n-close-color-pressed":no,"--n-close-color-hover":co,"--n-close-border-radius":Y,"--n-close-icon-color":N,"--n-close-icon-color-hover":lo,"--n-close-icon-color-pressed":ao,"--n-close-icon-color-disabled":N,"--n-close-margin-top":y.top,"--n-close-margin-right":y.right,"--n-close-margin-bottom":y.bottom,"--n-close-margin-left":y.left,"--n-close-size":G,"--n-color":a||(e.value?q:oo),"--n-color-checkable":j,"--n-color-checked":L,"--n-color-checked-hover":A,"--n-color-checked-pressed":Q,"--n-color-hover-checkable":w,"--n-color-pressed-checkable":K,"--n-font-size":X,"--n-height":F,"--n-opacity-disabled":M,"--n-padding":H,"--n-text-color":C||eo,"--n-text-color-checkable":_,"--n-text-color-checked":W,"--n-text-color-hover-checkable":E,"--n-text-color-pressed-checkable":O}}),v=c?vo("tag",S(()=>{let o="";const{type:a,color:{color:C,textColor:g}={}}=l;return o+=a[0],o+=s.value[0],C&&(o+=`a${V(C)}`),g&&(o+=`b${V(g)}`),e.value&&(o+="c"),o}),P,l):void 0;return Object.assign(Object.assign({},b),{rtlEnabled:B,mergedClsPrefix:f,contentRef:t,mergedBordered:e,handleClick:p,handleCloseClick:k,cssVars:c?void 0:P,themeClass:v==null?void 0:v.themeClass,onRender:v==null?void 0:v.onRender})},render(){var l,t;const{mergedClsPrefix:e,rtlEnabled:f,closable:c,color:{borderColor:d}={},round:n,onRender:s,$slots:i}=this;s==null||s();const p=U(i.avatar,b=>b&&x("div",{class:`${e}-tag__avatar`},b)),k=U(i.icon,b=>b&&x("div",{class:`${e}-tag__icon`},b));return x("div",{class:[`${e}-tag`,this.themeClass,{[`${e}-tag--rtl`]:f,[`${e}-tag--strong`]:this.strong,[`${e}-tag--disabled`]:this.disabled,[`${e}-tag--checkable`]:this.checkable,[`${e}-tag--checked`]:this.checkable&&this.checked,[`${e}-tag--round`]:n,[`${e}-tag--avatar`]:p,[`${e}-tag--icon`]:k,[`${e}-tag--closable`]:c}],style:this.cssVars,onClick:this.handleClick,onMouseenter:this.onMouseenter,onMouseleave:this.onMouseleave},k||p,x("span",{class:`${e}-tag__content`,ref:"contentRef"},(t=(l=this.$slots).default)===null||t===void 0?void 0:t.call(l)),!this.checkable&&c?x(go,{clsPrefix:e,class:`${e}-tag__close`,disabled:this.disabled,onClick:this.handleCloseClick,focusable:this.internalCloseFocusable,round:n,isButtonTag:this.internalCloseIsButtonTag,absolute:!0}):null,!this.checkable&&this.mergedBordered?x("div",{class:`${e}-tag__border`,style:{borderColor:d}}):null)}});export{Ro as N,Io as c,yo as t,Ho as u};

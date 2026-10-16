import React, { lazy, memo } from 'react';

export default lazy(async () => {
  const [{default:Markdown},{default:remarkGfm}]=await Promise.all([import('react-markdown'),import('remark-gfm')]);
  return {default:memo(function MarkdownView({text}:{text:string}) {
  if(text.length>200000) return <p className="note">原文较大，渲染格式最多支持 20 万字符；请使用原格式编辑。</p>;
  return <Markdown remarkPlugins={[remarkGfm]} skipHtml components={{
    img:({alt}) => <span className="md-image-note">[图片：{alt || '外部图片未自动加载'}]</span>,
    a:({children,href}) => <span className="md-link" title={href}>{children}</span>,
  }}>{text}</Markdown>;
  })};
});

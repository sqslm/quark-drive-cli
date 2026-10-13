import React, { createContext, useContext, useEffect, useRef, useState } from 'react';

export type Tone = 'success' | 'error' | 'warning' | 'info';
export type Feedback = (text:string,tone?:Tone) => void;
const Context = createContext<Feedback | null>(null);
export function useFeedback() {
  const notify = useContext(Context);
  if(!notify) throw new Error('Feedback provider missing');
  return notify;
}
export function FeedbackProvider({children}:{children:React.ReactNode}) {
  const [toast,setToast] = useState<{id:number;text:string;tone:Tone}|null>(null);
  const sequence = useRef(0);
  useEffect(() => {
    if(!toast) return;
    const timer = window.setTimeout(() => setToast(null),toast.tone === 'error' || toast.tone === 'warning' ? 6500 : 3500);
    return () => window.clearTimeout(timer);
  },[toast]);
  const notify:Feedback = (text,tone='info') => setToast({id:++sequence.current,text,tone});
  return <Context.Provider value={notify}>{children}{toast && <div key={toast.id} className={`toast ${toast.tone}`} role={toast.tone === 'error' ? 'alert' : 'status'} aria-live={toast.tone === 'error' ? 'assertive' : 'polite'} aria-atomic="true">
    <span className="toast-symbol" aria-hidden="true">{toast.tone === 'success' ? '✓' : toast.tone === 'error' ? '!' : 'i'}</span><span>{toast.text}</span><button aria-label="关闭提示" onClick={() => setToast(null)}>×</button>
  </div>}</Context.Provider>;
}

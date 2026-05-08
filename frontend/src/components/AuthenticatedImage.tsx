import { useState, useEffect, useRef } from 'react';
import { apiService } from '../services/api';

interface AuthenticatedImageProps extends Omit<React.ImgHTMLAttributes<HTMLImageElement>, 'src'> {
  src: string;
  fallback?: React.ReactNode;
}

/**
 * 对同源 /api/ 图片使用 fetch + Authorization + Blob URL 渲染，
 * 对 data:、blob:、外部图片直接渲染。
 */
export function AuthenticatedImage({ src, fallback, alt, onError, ...imgProps }: AuthenticatedImageProps) {
  const [blobUrl, setBlobUrl] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);
  const abortRef = useRef<AbortController | null>(null);
  const blobUrlRef = useRef<string | null>(null);

  const needsAuth = src.startsWith('/api/');

  useEffect(() => {
    if (!needsAuth) return;

    setFailed(false);
    setBlobUrl(null);
    if (blobUrlRef.current) {
      URL.revokeObjectURL(blobUrlRef.current);
      blobUrlRef.current = null;
    }

    const controller = new AbortController();
    abortRef.current = controller;

    fetch(src, {
      headers: apiService.getAuthHeaders(),
      signal: controller.signal,
    })
      .then((res) => {
        if (!res.ok) throw new Error(`${res.status}`);
        return res.blob();
      })
      .then((blob) => {
        if (controller.signal.aborted) return;
        const url = URL.createObjectURL(blob);
        blobUrlRef.current = url;
        setBlobUrl(url);
      })
      .catch((err) => {
        if (err.name === 'AbortError') return;
        setFailed(true);
      });

    return () => {
      controller.abort();
      if (blobUrlRef.current) {
        URL.revokeObjectURL(blobUrlRef.current);
        blobUrlRef.current = null;
      }
    };
  }, [src, needsAuth]);

  if (needsAuth) {
    if (failed) return <>{fallback ?? null}</>;
    if (!blobUrl) return null; // loading
    return (
      <img
        {...imgProps}
        src={blobUrl}
        alt={alt}
        onError={(e) => {
          setFailed(true);
          onError?.(e);
        }}
      />
    );
  }

  // 非 API 图片直接渲染
  return (
    <img
      {...imgProps}
      src={src}
      alt={alt}
      onError={(e) => {
        setFailed(true);
        onError?.(e);
      }}
    />
  );
}

import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  AlertCircle,
  BookOpen,
  ChevronDown,
  ChevronUp,
  FileText,
  GripVertical,
  Link,
  Link2Off,
  PanelLeftClose,
  PanelLeftOpen,
  RefreshCw,
  UploadCloud,
  ZoomIn,
  ZoomOut,
} from 'lucide-react';
import { Document, Page, pdfjs } from 'react-pdf';
import 'react-pdf/dist/Page/AnnotationLayer.css';
import 'react-pdf/dist/Page/TextLayer.css';

pdfjs.GlobalWorkerOptions.workerSrc = new URL(
  'pdfjs-dist/build/pdf.worker.min.mjs',
  import.meta.url,
).toString();

interface JobProgress {
  pages: number;
  blocks: number;
  units: number;
  batches_total: number;
  batches_completed: number;
}

interface Job {
  id: string;
  filename: string;
  original_path: string;
  translated_path: string | null;
  status: 'queued' | 'running' | 'done' | 'failed';
  progress: JobProgress;
  logs: string[];
  error: string | null;
  created_at: number;
}

function mergeJobs(current: Job[], incoming: Job[]) {
  const byId = new Map(current.map((job) => [job.id, job]));
  incoming.forEach((job) => byId.set(job.id, job));
  return Array.from(byId.values()).sort((a, b) => b.created_at - a.created_at);
}

function statusText(status: Job['status']) {
  if (status === 'queued') return '排队中';
  if (status === 'running') return '翻译中';
  if (status === 'done') return '已完成';
  return '失败';
}

function formatCreatedAt(timestamp: number) {
  return new Date(timestamp * 1000).toLocaleString('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  });
}

function App() {
  const [jobs, setJobs] = useState<Job[]>([]);
  const [activeJobId, setActiveJobId] = useState<string | null>(null);
  const [jobEventVersion, setJobEventVersion] = useState(0);

  const [isSidebarCollapsed, setIsSidebarCollapsed] = useState(false);
  const [isMonitorCollapsed, setIsMonitorCollapsed] = useState(false);
  const [leftPanelWidth, setLeftPanelWidth] = useState(50);

  const [isSyncScroll, setIsSyncScroll] = useState(true);
  const [scale, setScale] = useState(1.1);
  const [totalPages, setTotalPages] = useState(1);
  const [autoMonitorJobId, setAutoMonitorJobId] = useState<string | null>(null);

  const [isDragging, setIsDragging] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const dualPanelRef = useRef<HTMLDivElement>(null);
  const leftViewportRef = useRef<HTMLDivElement>(null);
  const rightViewportRef = useRef<HTMLDivElement>(null);
  const logTerminalRef = useRef<HTMLDivElement>(null);
  const logEndRef = useRef<HTMLDivElement>(null);

  const isSyncingLeft = useRef(false);
  const isSyncingRight = useRef(false);

  const activeJob = jobs.find((job) => job.id === activeJobId) || null;
  const pageNumbers = useMemo(() => Array.from({ length: totalPages }, (_, index) => index + 1), [totalPages]);
  const originalPdfUrl = activeJob
    ? `/api/files/original/${encodeURIComponent(activeJob.filename)}`
    : null;
  const translatedPdfName = activeJob?.translated_path?.split(/[\\/]/).pop() || '';
  const translatedPdfUrl = translatedPdfName
    ? `/api/files/translated/${encodeURIComponent(translatedPdfName)}`
    : null;

  const fetchJobs = useCallback(async () => {
    try {
      const res = await fetch('/api/jobs');
      if (res.ok) {
        const data: Job[] = await res.json();
        setJobs(data);

        if (data.length > 0 && (!activeJobId || !data.some((job) => job.id === activeJobId))) {
          setTotalPages(1);
          setActiveJobId(data[0].id);
        }
      }
    } catch (err) {
      console.error('Failed to fetch jobs:', err);
    }
  }, [activeJobId, jobEventVersion]);

  useEffect(() => {
    queueMicrotask(fetchJobs);
    const interval = setInterval(fetchJobs, 4000);
    return () => clearInterval(interval);
  }, [fetchJobs]);

  useEffect(() => {
    if (!activeJobId) return;

    fetch(`/api/jobs/${activeJobId}`)
      .then((res) => (res.ok ? res.json() : null))
      .then((data) => {
        if (data) {
          setJobs((prev) => prev.map((job) => (job.id === data.id ? data : job)));
        }
      })
      .catch((err) => console.error('Error fetching job details:', err));

    const eventSource = new EventSource(`/api/jobs/${activeJobId}/events`);

    eventSource.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        if (data.error === 'Job not found') {
          eventSource.close();
          return;
        }

        setJobs((prev) => prev.map((job) => (job.id === data.id ? { ...job, ...data } : job)));

        if (data.status === 'done' || data.status === 'failed') {
          eventSource.close();
        }
      } catch (e) {
        console.error('Failed to parse SSE data:', e);
      }
    };

    eventSource.onerror = () => {
      eventSource.close();
    };

    return () => {
      eventSource.close();
    };
  }, [activeJobId, jobEventVersion]);

  useEffect(() => {
    logEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [activeJob?.logs.length, isMonitorCollapsed]);

  useEffect(() => {
    if (!activeJob || activeJob.id !== autoMonitorJobId) return;
    if (activeJob.status !== 'done' && activeJob.status !== 'failed') return;
    setIsMonitorCollapsed(true);
    setAutoMonitorJobId(null);
  }, [activeJob, autoMonitorJobId]);

  useEffect(() => {
    const handleWheel = (event: WheelEvent) => {
      if (!event.ctrlKey) return;
      event.preventDefault();
      setScale((value) => Math.min(2.5, Math.max(0.6, value + (event.deltaY < 0 ? 0.15 : -0.15))));
    };
    const options = { passive: false, capture: true };
    const leftViewport = leftViewportRef.current;
    const rightViewport = rightViewportRef.current;

    leftViewport?.addEventListener('wheel', handleWheel, options);
    rightViewport?.addEventListener('wheel', handleWheel, options);

    return () => {
      leftViewport?.removeEventListener('wheel', handleWheel, options);
      rightViewport?.removeEventListener('wheel', handleWheel, options);
    };
  }, [activeJobId]);

  const handleLeftScroll = () => {
    if (!isSyncScroll) return;
    if (isSyncingLeft.current) {
      isSyncingLeft.current = false;
      return;
    }
    if (!rightViewportRef.current || !leftViewportRef.current) return;
    isSyncingRight.current = true;
    rightViewportRef.current.scrollTop = leftViewportRef.current.scrollTop;
    rightViewportRef.current.scrollLeft = leftViewportRef.current.scrollLeft;
  };

  const handleRightScroll = () => {
    if (!isSyncScroll) return;
    if (isSyncingRight.current) {
      isSyncingRight.current = false;
      return;
    }
    if (!rightViewportRef.current || !leftViewportRef.current) return;
    isSyncingLeft.current = true;
    leftViewportRef.current.scrollTop = rightViewportRef.current.scrollTop;
    leftViewportRef.current.scrollLeft = rightViewportRef.current.scrollLeft;
  };

  const handleDividerPointerDown = (event: React.PointerEvent<HTMLButtonElement>) => {
    const container = dualPanelRef.current;
    if (!container) return;

    event.preventDefault();
    const rect = container.getBoundingClientRect();

    const updateWidth = (clientX: number) => {
      const nextWidth = ((clientX - rect.left) / rect.width) * 100;
      setLeftPanelWidth(Math.min(78, Math.max(22, nextWidth)));
    };

    const handlePointerMove = (moveEvent: PointerEvent) => {
      updateWidth(moveEvent.clientX);
    };

    const handlePointerUp = () => {
      document.body.classList.remove('resizing-panels');
      document.removeEventListener('pointermove', handlePointerMove);
      document.removeEventListener('pointerup', handlePointerUp);
    };

    document.body.classList.add('resizing-panels');
    updateWidth(event.clientX);
    document.addEventListener('pointermove', handlePointerMove);
    document.addEventListener('pointerup', handlePointerUp);
  };

  const handleMonitorToggle = () => {
    setAutoMonitorJobId(null);
    setIsMonitorCollapsed((collapsed) => !collapsed);
  };

  const handleDragOver = (e: React.DragEvent) => {
    e.preventDefault();
    setIsDragging(true);
  };

  const handleDragLeave = () => {
    setIsDragging(false);
  };

  const handleDrop = async (e: React.DragEvent) => {
    e.preventDefault();
    setIsDragging(false);

    const files = Array.from(e.dataTransfer.files).filter((file) => file.name.toLowerCase().endsWith('.pdf'));
    if (files.length === 0) return;

    uploadFiles(files);
  };

  const handleFileSelect = (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = e.target.files ? Array.from(e.target.files) : [];
    if (files.length === 0) return;
    uploadFiles(files);
    e.target.value = '';
  };

  const uploadFiles = async (files: File[]) => {
    const formData = new FormData();
    files.forEach((file) => {
      formData.append('files', file);
    });

    try {
      const res = await fetch('/api/jobs', {
        method: 'POST',
        body: formData,
      });
      if (res.ok) {
        const data: Job[] = await res.json();
        setJobs((prev) => mergeJobs(prev, data));
        if (data.length > 0) {
          setTotalPages(1);
          setAutoMonitorJobId(data[0].id);
          setIsMonitorCollapsed(false);
          setActiveJobId(data[0].id);
        }
      }
    } catch (err) {
      console.error('Upload failed:', err);
    }
  };

  const handleRetranslate = async () => {
    if (!activeJob || activeJob.status === 'queued' || activeJob.status === 'running') return;

    try {
      const res = await fetch(`/api/jobs/${activeJob.id}/retranslate`, { method: 'POST' });
      if (!res.ok) {
        const message = await res.text();
        setJobs((prev) =>
          prev.map((job) =>
            job.id === activeJob.id
              ? { ...job, status: 'failed', error: message || 'Retranslation request failed' }
              : job,
          ),
        );
        return;
      }

      const job: Job = await res.json();
      setJobs((prev) => mergeJobs(prev, [job]));
      setActiveJobId(job.id);
      setJobEventVersion((version) => version + 1);
      setAutoMonitorJobId(job.id);
      setIsMonitorCollapsed(false);
    } catch (err) {
      console.error('Retranslation failed:', err);
    }
  };

  const getJobProgressPercent = (job: Job) => {
    if (job.status === 'done' || job.status === 'failed') return 100;
    if (job.progress.batches_total === 0) return 0;
    return Math.round((job.progress.batches_completed / job.progress.batches_total) * 100);
  };

  return (
    <div className="workbench-container" onDragOver={handleDragOver} onDragLeave={handleDragLeave} onDrop={handleDrop}>
      <aside className={`sidebar ${isSidebarCollapsed ? 'collapsed' : ''}`}>
        <div className="sidebar-header">
          <div className="brand-lockup">
            <div className="logo-badge">PDF</div>
            <h1>
              文献翻修工作台
              <span>v1.0</span>
            </h1>
          </div>
          <button
            type="button"
            className="icon-button"
            onClick={() => setIsSidebarCollapsed((collapsed) => !collapsed)}
            title={isSidebarCollapsed ? '展开左侧工作栏' : '收起左侧工作栏'}
            aria-label={isSidebarCollapsed ? '展开左侧工作栏' : '收起左侧工作栏'}
          >
            {isSidebarCollapsed ? <PanelLeftOpen size={18} /> : <PanelLeftClose size={18} />}
          </button>
        </div>

        {!isSidebarCollapsed && (
          <div className="sidebar-body">
            <div className={`upload-zone ${isDragging ? 'drag-active' : ''}`} onClick={() => fileInputRef.current?.click()}>
              <input type="file" ref={fileInputRef} onChange={handleFileSelect} multiple accept=".pdf" />
              <UploadCloud className="upload-icon" size={30} />
              <p>拖拽论文至此处上传</p>
              <p className="subtitle">支持批量 PDF；重复文件会复用已有任务</p>
            </div>

            <div className="job-list">
              {jobs.length === 0 ? (
                <div className="job-empty">暂无任务</div>
              ) : (
                jobs.map((job) => (
                  <button
                    type="button"
                    key={job.id}
                    className={`job-card ${activeJobId === job.id ? 'active' : ''}`}
                    onClick={() => {
                      if (activeJobId !== job.id) {
                        setTotalPages(1);
                      }
                      setActiveJobId(job.id);
                    }}
                  >
                    <div className="job-card-header">
                      <FileText size={16} />
                      <span className="job-name" title={job.filename}>
                        {job.filename}
                      </span>
                    </div>
                    <div className="job-meta">
                      <span>{formatCreatedAt(job.created_at)}</span>
                    </div>
                  </button>
                ))
              )}
            </div>

            <section className={`monitor-panel ${isMonitorCollapsed ? 'collapsed' : ''}`}>
              <button
                type="button"
                className="monitor-header"
                onClick={handleMonitorToggle}
                aria-expanded={!isMonitorCollapsed}
              >
                <span>实时任务状态监控</span>
                {isMonitorCollapsed ? <ChevronUp size={16} /> : <ChevronDown size={16} />}
              </button>

              {!isMonitorCollapsed && (
                <div className="monitor-body">
                  {activeJob ? (
                    <>
                      <div className="monitor-status-row">
                        <span className={`monitor-status ${activeJob.status}`}>{statusText(activeJob.status)}</span>
                        <span className="monitor-percent">{getJobProgressPercent(activeJob)}%</span>
                      </div>
                      <div className="job-progress-bar-bg">
                        <div
                          className={`job-progress-bar ${activeJob.status}`}
                          style={{ width: `${getJobProgressPercent(activeJob)}%` }}
                        />
                      </div>
                      <div className="stats-grid">
                        <div className="stat-item">
                          <span>页数</span>
                          <span className="stat-val">{activeJob.progress.pages}</span>
                        </div>
                        <div className="stat-item">
                          <span>文本块</span>
                          <span className="stat-val">{activeJob.progress.blocks}</span>
                        </div>
                        <div className="stat-item">
                          <span>翻译单元</span>
                          <span className="stat-val">{activeJob.progress.units}</span>
                        </div>
                        <div className="stat-item">
                          <span>批次</span>
                          <span className="stat-val">
                            {activeJob.progress.batches_completed} / {activeJob.progress.batches_total}
                          </span>
                        </div>
                      </div>

                      <div className="log-terminal" ref={logTerminalRef}>
                        {activeJob.logs.map((log, index) => {
                          let className = 'log-line';
                          if (log.includes('ERROR') || log.includes('FAILED') || log.includes('CRITICAL')) {
                            className += ' error';
                          } else if (log.includes('saved:') || log.includes('Done.')) {
                            className += ' success';
                          } else if (log.startsWith('Job') || log.includes('PDF:') || log.includes('OUT:')) {
                            className += ' system';
                          }
                          return (
                            <div key={index} className={className}>
                              {log}
                            </div>
                          );
                        })}
                        <div ref={logEndRef} />
                      </div>
                    </>
                  ) : (
                    <div className="monitor-empty">选择左侧论文后显示任务详情</div>
                  )}
                </div>
              )}
            </section>
          </div>
        )}
      </aside>

      <main className="main-workbench">
        {activeJob ? (
          <>
            <div className="top-bar">
              <div className="active-job-title">
                <FileText size={18} />
                <span className="filename" title={activeJob.filename}>
                  {activeJob.filename}
                </span>
                <button
                  type="button"
                  className="control-btn title-action"
                  onClick={handleRetranslate}
                  disabled={activeJob.status === 'queued' || activeJob.status === 'running'}
                >
                  <RefreshCw size={15} />
                  重新翻译
                </button>
              </div>

              <div className="pdf-controls">
                <span className="page-indicator">
                  共 {totalPages || 1} 页
                </span>

                <button type="button" className="control-btn icon-only" onClick={() => setScale((value) => Math.max(0.6, value - 0.1))}>
                  <ZoomOut size={16} />
                </button>
                <span className="zoom-indicator">{Math.round(scale * 100)}%</span>
                <button type="button" className="control-btn icon-only" onClick={() => setScale((value) => Math.min(2.5, value + 0.1))}>
                  <ZoomIn size={16} />
                </button>

                <div className="sync-toggle">
                  {isSyncScroll ? <Link size={16} /> : <Link2Off size={16} />}
                  <span>滚动同步</span>
                  <label className="switch">
                    <input type="checkbox" checked={isSyncScroll} onChange={(e) => setIsSyncScroll(e.target.checked)} />
                    <span className="slider"></span>
                  </label>
                </div>
              </div>
            </div>

            <div className="dual-panel-view" ref={dualPanelRef}>
              <div className="pdf-panel" style={{ flex: `${leftPanelWidth} 1 0` }}>
                <div className="panel-header">
                  <span>英文原文</span>
                  <span>{totalPages || 1} PAGES</span>
                </div>
                <div className="pdf-viewport" ref={leftViewportRef} onScroll={handleLeftScroll}>
                  {originalPdfUrl && (
                    <Document
                      file={originalPdfUrl}
                      className="pdf-document"
                      loading={
                        <div className="watermark-overlay">
                          <RefreshCw size={32} className="upload-icon" />
                          <p>正在读取原文 PDF 结构</p>
                        </div>
                      }
                      error={
                        <div className="watermark-overlay">
                          <AlertCircle size={40} />
                          <p>原文 PDF 加载失败</p>
                        </div>
                      }
                      onLoadSuccess={({ numPages }) => setTotalPages(numPages)}
                    >
                      {pageNumbers.map((page) => (
                        <Page key={page} pageNumber={page} scale={scale} className="pdf-page-shadow" />
                      ))}
                    </Document>
                  )}
                </div>
              </div>

              <button
                type="button"
                className="panel-divider"
                onPointerDown={handleDividerPointerDown}
                aria-label="调整左右阅读区宽度"
                title="拖动调整左右阅读区宽度"
              >
                <GripVertical size={16} />
              </button>

              <div className="pdf-panel" style={{ flex: `${100 - leftPanelWidth} 1 0` }}>
                <div className="panel-header">
                  <span>中文译文</span>
                  <span>{totalPages || 1} PAGES</span>
                </div>
                <div className="pdf-viewport" ref={rightViewportRef} onScroll={handleRightScroll}>
                  {activeJob.status === 'done' && translatedPdfUrl && translatedPdfName ? (
                    <Document
                      file={translatedPdfUrl}
                      className="pdf-document"
                      loading={
                        <div className="watermark-overlay">
                          <RefreshCw size={32} className="upload-icon" />
                          <p>正在读取译文 PDF 结构</p>
                        </div>
                      }
                      error={
                        <div className="watermark-overlay">
                          <AlertCircle size={40} />
                          <h3>译文 PDF 读取失败</h3>
                          <p>服务器已生成 {translatedPdfName}，但浏览器无法打开。请重新翻译，或检查文件是否损坏。</p>
                        </div>
                      }
                    >
                      {pageNumbers.map((page) => (
                        <Page key={page} pageNumber={page} scale={scale} className="pdf-page-shadow" />
                      ))}
                    </Document>
                  ) : activeJob.status === 'failed' ? (
                    <div className="watermark-overlay">
                      <AlertCircle size={40} />
                      <h3>翻译失败，未生成译文 PDF</h3>
                      <p>{activeJob.error || '查看左侧日志中的 CRITICAL ERROR，修正后可重新翻译。'}</p>
                    </div>
                  ) : activeJob.status === 'done' ? (
                    <div className="watermark-overlay">
                      <AlertCircle size={40} />
                      <h3>任务已完成，但没有译文文件路径</h3>
                      <p>后端没有返回译文 PDF 文件名。请刷新任务列表；如果仍然缺失，请重新翻译。</p>
                    </div>
                  ) : (
                    <div className="watermark-overlay">
                      <BookOpen size={40} />
                      <h3>{activeJob.status === 'queued' ? '译文尚未开始生成' : '正在翻译，译文 PDF 尚未生成'}</h3>
                      <p>翻译和排版全部完成后，这里会显示中文译文 PDF；当前进度见左侧监控区。</p>
                    </div>
                  )}
                </div>
              </div>
            </div>
          </>
        ) : (
          <div className="empty-workbench">
            <BookOpen size={48} />
            <h3>文献审校工作台空闲</h3>
            <p>上传 PDF，或从左侧选择已处理过的论文。</p>
          </div>
        )}
      </main>
    </div>
  );
}

export default App;

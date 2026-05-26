import React, { useState, useEffect, useRef, useCallback } from 'react';
import {
  UploadCloud,
  FileText,
  RefreshCw,
  ChevronsLeft,
  ChevronsRight,
  ZoomIn,
  ZoomOut,
  Link,
  Link2Off,
  AlertCircle,
  BookOpen
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

function App() {
  const [jobs, setJobs] = useState<Job[]>([]);
  const [activeJobId, setActiveJobId] = useState<string | null>(null);
  const [filter, setFilter] = useState<'all' | 'queued' | 'running' | 'done' | 'failed'>('all');
  
  const [isSyncScroll, setIsSyncScroll] = useState(true);
  const [scale, setScale] = useState(1.1);
  const [pageNumber, setPageNumber] = useState(1);
  const [totalPages, setTotalPages] = useState(1);
  
  const [isDragging, setIsDragging] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);
  
  const leftViewportRef = useRef<HTMLDivElement>(null);
  const rightViewportRef = useRef<HTMLDivElement>(null);
  const logTerminalRef = useRef<HTMLDivElement>(null);
  const logEndRef = useRef<HTMLDivElement>(null);
  
  const isSyncingLeft = useRef(false);
  const isSyncingRight = useRef(false);

  const activeJob = jobs.find(j => j.id === activeJobId) || null;
  const originalPdfUrl = activeJob
    ? `/api/files/original/${encodeURIComponent(activeJob.filename)}`
    : null;
  const translatedPdfUrl = activeJob?.translated_path
    ? `/api/files/translated/${encodeURIComponent(activeJob.translated_path.split(/[\\/]/).pop() || '')}`
    : null;

  // Fetch initial job list
  const fetchJobs = useCallback(async () => {
    try {
      const res = await fetch('/api/jobs');
      if (res.ok) {
        const data = await res.json();
        setJobs(data);
        // If there is no active job selected yet, pick the first one
        if (data.length > 0 && !activeJobId) {
          setPageNumber(1);
          setTotalPages(1);
          setActiveJobId(data[0].id);
        }
      }
    } catch (err) {
      console.error("Failed to fetch jobs:", err);
    }
  }, [activeJobId]);

  useEffect(() => {
    queueMicrotask(fetchJobs);
    // Poll job list every 4 seconds to catch updates to background queues in other tabs
    const interval = setInterval(fetchJobs, 4000);
    return () => clearInterval(interval);
  }, [fetchJobs]);

  // Set up EventSource for real-time SSE updates on active job
  useEffect(() => {
    if (!activeJobId) return;

    // Fetch details once immediately
    fetch(`/api/jobs/${activeJobId}`)
      .then(res => res.ok ? res.json() : null)
      .then(data => {
        if (data) {
          setJobs(prev => prev.map(j => j.id === data.id ? data : j));
        }
      })
      .catch(err => console.error("Error fetching job details:", err));

    // Establish Server-Sent Events stream
    const eventSource = new EventSource(`/api/jobs/${activeJobId}/events`);

    eventSource.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        if (data.error && data.error === 'Job not found') {
          eventSource.close();
          return;
        }

        setJobs(prev => prev.map(j => j.id === data.id ? { ...j, ...data } : j));

        // If finished, close SSE stream
        if (data.status === 'done' || data.status === 'failed') {
          eventSource.close();
        }
      } catch (e) {
        console.error("Failed to parse SSE data:", e);
      }
    };

    eventSource.onerror = () => {
      eventSource.close();
    };

    return () => {
      eventSource.close();
    };
  }, [activeJobId]);

  // Autoscroll terminal when log line count increases
  useEffect(() => {
    if (logEndRef.current) {
      logEndRef.current.scrollIntoView({ behavior: 'smooth' });
    }
  }, [activeJob?.logs?.length]);

  // Sync scroll positions using guard variables
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

  // Drag and drop handlers
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

    const files = Array.from(e.dataTransfer.files).filter(f => f.name.toLowerCase().endsWith('.pdf'));
    if (files.length === 0) return;

    uploadFiles(files);
  };

  const handleFileSelect = (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = e.target.files ? Array.from(e.target.files) : [];
    if (files.length === 0) return;
    uploadFiles(files);
  };

  const uploadFiles = async (files: File[]) => {
    const formData = new FormData();
    files.forEach(file => {
      formData.append('files', file);
    });

    try {
      const res = await fetch('/api/jobs', {
        method: 'POST',
        body: formData,
      });
      if (res.ok) {
        const data = await res.json();
        setJobs(prev => [...data, ...prev]);
        if (data.length > 0) {
          setPageNumber(1);
          setTotalPages(1);
          setActiveJobId(data[0].id);
        }
      }
    } catch (err) {
      console.error("Upload failed:", err);
    }
  };

  // Filtered jobs for sidebar list
  const filteredJobs = jobs.filter(job => {
    if (filter === 'all') return true;
    return job.status === filter;
  });

  // Helper formats
  const getJobProgressPercent = (job: Job) => {
    if (job.status === 'done') return 100;
    if (job.status === 'failed') return 100;
    if (job.progress.batches_total === 0) return 0;
    return Math.round((job.progress.batches_completed / job.progress.batches_total) * 100);
  };

  return (
    <div className="workbench-container" onDragOver={handleDragOver} onDragLeave={handleDragLeave} onDrop={handleDrop}>
      {/* Sidebar - Task Queue & Upload */}
      <aside className="sidebar">
        <div className="sidebar-header">
          <div className="logo-badge">PDF</div>
          <h1>文献翻修工作台 <span style={{ fontSize: '0.75rem', fontWeight: 400, color: 'var(--text-secondary)' }}>v1.0</span></h1>
        </div>

        {/* Drag and Drop Zone */}
        <div className={`upload-zone ${isDragging ? 'drag-active' : ''}`} onClick={() => fileInputRef.current?.click()}>
          <input type="file" ref={fileInputRef} onChange={handleFileSelect} multiple accept=".pdf" style={{ display: 'none' }} />
          <UploadCloud className="upload-icon" size={32} />
          <p style={{ fontWeight: 600 }}>拖拽论文至此处上传</p>
          <p className="subtitle">支持批量 PDF，文件同名自动保护</p>
        </div>

        {/* Tab Filters */}
        <div className="job-filters">
          {(['all', 'queued', 'running', 'done', 'failed'] as const).map((status) => (
            <button
              key={status}
              className={`filter-btn ${filter === status ? 'active' : ''}`}
              onClick={() => setFilter(status)}
            >
              {status === 'all' ? '全部' : 
               status === 'queued' ? '排队' :
               status === 'running' ? '翻译中' :
               status === 'done' ? '完成' : '失败'}
            </button>
          ))}
        </div>

        {/* Job Cards Queue */}
        <div className="job-list">
          {filteredJobs.length === 0 ? (
            <div style={{ textAlign: 'center', padding: '40px 10px', color: 'var(--text-muted)', fontSize: '0.85rem' }}>
              无对应任务
            </div>
          ) : (
            filteredJobs.map((job) => (
              <div
                key={job.id}
                className={`job-card ${activeJobId === job.id ? 'active' : ''}`}
                onClick={() => {
                  if (activeJobId !== job.id) {
                    setPageNumber(1);
                    setTotalPages(1);
                  }
                  setActiveJobId(job.id);
                }}
              >
                <div className="job-card-header">
                  <span className="job-name" title={job.filename}>{job.filename}</span>
                  <span className={`badge ${job.status}`}>
                    {job.status === 'queued' ? '排队中' :
                     job.status === 'running' ? '翻译中' :
                     job.status === 'done' ? '已完成' : '失败'}
                  </span>
                </div>
                <div className="job-meta">
                  <span>大小: {job.progress.pages > 0 ? `${job.progress.pages} 页` : '等待解析'}</span>
                  {job.status === 'running' && (
                    <span>进度: {getJobProgressPercent(job)}%</span>
                  )}
                </div>
                
                {/* Progress bar */}
                {(job.status === 'running' || job.status === 'done' || job.status === 'failed') && (
                  <div className="job-progress-bar-bg">
                    <div
                      className={`job-progress-bar ${job.status}`}
                      style={{ width: `${getJobProgressPercent(job)}%` }}
                    />
                  </div>
                )}

                {job.status === 'failed' && job.error && (
                  <div className="job-error-msg">{job.error}</div>
                )}
              </div>
            ))
          )}
        </div>
      </aside>

      {/* Main Review and Control Desk */}
      <main className="main-workbench">
        {activeJob ? (
          <>
            {/* Top Navigation Control Bar */}
            <div className="top-bar">
              <div className="active-job-title">
                <FileText size={18} style={{ color: 'var(--accent-color)' }} />
                <span className="filename" title={activeJob.filename}>{activeJob.filename}</span>
              </div>

              <div className="pdf-controls">
                {/* Page Navigation */}
                <button
                  className="control-btn"
                  onClick={() => setPageNumber(p => Math.max(1, p - 1))}
                  disabled={pageNumber <= 1}
                >
                  <ChevronsLeft size={16} />
                  上一页
                </button>
                <span className="page-indicator">
                  第 {pageNumber} / {totalPages || 1} 页
                </span>
                <button
                  className="control-btn"
                  onClick={() => setPageNumber(p => Math.min(totalPages, p + 1))}
                  disabled={pageNumber >= totalPages}
                >
                  下一页
                  <ChevronsRight size={16} />
                </button>

                {/* Zoom controls */}
                <button className="control-btn" onClick={() => setScale(s => Math.max(0.6, s - 0.1))}>
                  <ZoomOut size={16} />
                </button>
                <span style={{ fontSize: '0.8rem', color: 'var(--text-secondary)', fontFamily: 'var(--mono-font)', width: '36px', textAlign: 'center' }}>
                  {Math.round(scale * 100)}%
                </span>
                <button className="control-btn" onClick={() => setScale(s => Math.min(2.5, s + 0.1))}>
                  <ZoomIn size={16} />
                </button>

                {/* Synchronized Scrolling Toggle */}
                <div className="sync-toggle">
                  {isSyncScroll ? <Link size={16} style={{ color: 'var(--success-color)' }} /> : <Link2Off size={16} />}
                  <span>滚动同步</span>
                  <label className="switch">
                    <input type="checkbox" checked={isSyncScroll} onChange={(e) => setIsSyncScroll(e.target.checked)} />
                    <span className="slider"></span>
                  </label>
                </div>
              </div>
            </div>

            {/* Split Screen Dual PDF Panel Preview */}
            <div className="dual-panel-view">
              {/* Left Panel: Original PDF */}
              <div className="pdf-panel">
                <div className="panel-header">
                  <span>英文原文</span>
                  <span>PAGE {pageNumber}</span>
                </div>
                <div className="pdf-viewport" ref={leftViewportRef} onScroll={handleLeftScroll}>
                  {originalPdfUrl && (
                    <Document
                      file={originalPdfUrl}
                      className="pdf-document"
                      loading={
                        <div className="watermark-overlay">
                          <RefreshCw size={32} className="upload-icon" />
                          <p>正在读取原文 PDF 结构，请稍候...</p>
                        </div>
                      }
                      error={
                        <div className="watermark-overlay" style={{ color: 'var(--danger-color)' }}>
                          <AlertCircle size={40} />
                          <p>原文 PDF 加载失败，请查看浏览器控制台或后端日志。</p>
                        </div>
                      }
                      onLoadSuccess={({ numPages }) => setTotalPages(numPages)}
                    >
                      <Page
                        pageNumber={pageNumber}
                        scale={scale}
                        className="pdf-page-shadow"
                      />
                    </Document>
                  )}
                </div>
              </div>

              <div className="panel-divider"></div>

              {/* Right Panel: Translated PDF */}
              <div className="pdf-panel">
                <div className="panel-header">
                  <span>中文译文 (SimSun / Times 字体对齐)</span>
                  <span>PAGE {pageNumber}</span>
                </div>
                <div className="pdf-viewport" ref={rightViewportRef} onScroll={handleRightScroll}>
                  {activeJob.status === 'done' && translatedPdfUrl ? (
                    <Document
                      file={translatedPdfUrl}
                      className="pdf-document"
                      loading={
                        <div className="watermark-overlay">
                          <RefreshCw size={32} className="upload-icon" />
                          <p>正在读取译文 PDF 结构，请稍候...</p>
                        </div>
                      }
                      error={
                        <div className="watermark-overlay" style={{ color: 'var(--danger-color)' }}>
                          <AlertCircle size={40} />
                          <p>译文 PDF 加载失败，请查看浏览器控制台或后端日志。</p>
                        </div>
                      }
                    >
                      <Page
                        pageNumber={pageNumber}
                        scale={scale}
                        className="pdf-page-shadow"
                      />
                    </Document>
                  ) : (
                    activeJob.status === 'failed' ? (
                      <div className="watermark-overlay" style={{ color: 'var(--danger-color)' }}>
                        <AlertCircle size={40} />
                        <h3>翻译失败</h3>
                        <p>{activeJob.error || '运行过程中出现未知异常，请查看下方控制台日志。'}</p>
                      </div>
                    ) : (
                      <div className="watermark-overlay">
                        <BookOpen size={40} />
                        <h3>译文生成中</h3>
                        <p>
                          {activeJob.status === 'running' 
                            ? `正在向 Google Translate 提交翻译批次并排版，当前已处理 ${activeJob.progress.batches_completed}/${activeJob.progress.batches_total} 个文本组...`
                            : '当前任务正在后端翻译队列中排队，请在下方实时查看系统进度日志。'}
                        </p>
                        {activeJob.status === 'running' && activeJob.progress.batches_total > 0 && (
                          <div style={{ width: '220px', height: '6px', backgroundColor: 'var(--border-color)', borderRadius: '99px', overflow: 'hidden' }}>
                            <div style={{ height: '100%', backgroundColor: 'var(--accent-color)', width: `${(activeJob.progress.batches_completed / activeJob.progress.batches_total) * 100}%`, transition: 'width 0.3s' }} />
                          </div>
                        )}
                      </div>
                    )
                  )}
                </div>
              </div>
            </div>

            {/* Bottom Real-time Statistics and Log Viewer */}
            <div className="bottom-bar">
              <div className="bottom-bar-header">
                <span>实时任务状态监控</span>
                <div className="stats-group">
                  <div className="stat-item">
                    <span>页数:</span>
                    <span className="stat-val">{activeJob.progress.pages}</span>
                  </div>
                  <div className="stat-item">
                    <span>文本块:</span>
                    <span className="stat-val">{activeJob.progress.blocks}</span>
                  </div>
                  <div className="stat-item">
                    <span>翻译单元:</span>
                    <span className="stat-val">{activeJob.progress.units}</span>
                  </div>
                  <div className="stat-item">
                    <span>批次进度:</span>
                    <span className="stat-val">{activeJob.progress.batches_completed} / {activeJob.progress.batches_total}</span>
                  </div>
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
            </div>
          </>
        ) : (
          <div className="empty-workbench">
            <BookOpen size={48} style={{ color: 'var(--text-muted)', opacity: 0.6 }} />
            <h3>文献审校工作台空闲</h3>
            <p>请在左侧上传一篇新的英文学术 PDF 论文，或者从队列列表中选择一个任务以开始原文/译文双栏同步审校。</p>
          </div>
        )}
      </main>
    </div>
  );
}

export default App;

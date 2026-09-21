import { UploadCloud } from 'lucide-react'
import { useRef, useState } from 'react'

export function Dropzone({ onFiles, disabled }) {
  const inputRef = useRef(null)
  const [isDragging, setIsDragging] = useState(false)

  function handleFiles(fileList) {
    const files = Array.from(fileList || [])
    if (files.length > 0) onFiles(files)
  }

  return (
    <div
      className={`dropzone${isDragging ? ' is-dragging' : ''}`}
      onDragOver={(e) => {
        e.preventDefault()
        if (!disabled) setIsDragging(true)
      }}
      onDragLeave={() => setIsDragging(false)}
      onDrop={(e) => {
        e.preventDefault()
        setIsDragging(false)
        if (!disabled) handleFiles(e.dataTransfer.files)
      }}
    >
      <div className="dropzone-icon">
        <UploadCloud size={26} />
      </div>
      <div className="dropzone-title">Drag & drop PDF files here</div>
      <div className="dropzone-subtitle">or</div>
      <button className="btn btn-primary" disabled={disabled} onClick={() => inputRef.current?.click()}>
        Choose Files
      </button>
      <input
        ref={inputRef}
        type="file"
        accept="application/pdf,.pdf"
        multiple
        hidden
        onChange={(e) => {
          handleFiles(e.target.files)
          e.target.value = ''
        }}
      />
    </div>
  )
}

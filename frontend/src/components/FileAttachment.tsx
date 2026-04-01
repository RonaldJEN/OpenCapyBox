import { FileText, FileImage, File, FileVideo, FileAudio, FileCode, FileArchive } from 'lucide-react';

export interface FileAttachmentProps {
  filename: string;
  size: string;
}

// 根据文件扩展名获取图标
function getFileIcon(filename: string) {
  const ext = filename.split('.').pop()?.toLowerCase() || '';

  // 图片文件
  if (['jpg', 'jpeg', 'png', 'gif', 'svg', 'webp', 'bmp'].includes(ext)) {
    return <FileImage className="w-5 h-5 text-blue-500" />;
  }

  // 视频文件
  if (['mp4', 'avi', 'mov', 'wmv', 'flv', 'mkv'].includes(ext)) {
    return <FileVideo className="w-5 h-5 text-purple-500" />;
  }

  // 音频文件
  if (['mp3', 'wav', 'ogg', 'flac', 'aac'].includes(ext)) {
    return <FileAudio className="w-5 h-5 text-green-500" />;
  }

  // 代码文件
  if (['js', 'jsx', 'ts', 'tsx', 'py', 'java', 'cpp', 'c', 'go', 'rs', 'rb', 'php'].includes(ext)) {
    return <FileCode className="w-5 h-5 text-orange-500" />;
  }

  // 压缩文件
  if (['zip', 'rar', '7z', 'tar', 'gz', 'bz2'].includes(ext)) {
    return <FileArchive className="w-5 h-5 text-yellow-500" />;
  }

  // 文档文件
  if (['pdf', 'doc', 'docx', 'txt', 'md', 'csv', 'xlsx', 'xls'].includes(ext)) {
    return <FileText className="w-5 h-5 text-red-500" />;
  }

  // 默认文件图标
  return <File className="w-5 h-5 text-gray-500" />;
}

export function FileAttachment({ filename, size }: FileAttachmentProps) {
  return (
    <div 
      data-testid="file-attachment"
      className="inline-flex items-center gap-2 px-3 py-2 bg-gray-100 hover:bg-gray-200 border border-gray-300 rounded-lg transition-colors cursor-default mr-2 mb-2"
    >
      {getFileIcon(filename)}
      <div className="flex flex-col">
        <span className="text-sm font-medium text-gray-700">{filename}</span>
        <span className="text-xs text-gray-500">{size}</span>
      </div>
    </div>
  );
}

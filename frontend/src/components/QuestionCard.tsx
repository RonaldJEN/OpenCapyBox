import { useState, useCallback, useMemo } from 'react';
import { MessageCircle, Check, Send, ChevronLeft, ChevronRight } from 'lucide-react';
import type { AskUserQuestion } from '../types';
import FeedbackMessage from './FeedbackMessage';

interface QuestionCardProps {
  questions: unknown;
  onSubmit: (answers: Record<string, string>) => void;
  disabled?: boolean;
}

const NO_PREFERENCE = '[No preference]';
const EMPTY_QUESTIONS: AskUserQuestion[] = [];

// Stored interactions can predate nested ask_user validation. Preserve each
// question/answer key, but never assume that its wire payload contains options.
function parseQuestions(input: unknown): AskUserQuestion[] | null {
  if (!Array.isArray(input)) return null;
  const result: AskUserQuestion[] = [];
  for (const [index, value] of input.entries()) {
    if (!value || typeof value !== 'object' || typeof value.question !== 'string' || !value.question.trim()) return null;
    const options = Array.isArray(value.options) ? value.options.flatMap((option: unknown) => {
      if (!option || typeof option !== 'object' || !('label' in option)
        || typeof option.label !== 'string' || !option.label.trim()) return [];
      return [{ label: option.label, description: 'description' in option && typeof option.description === 'string' ? option.description : '' }];
    }) : [];
    result.push({ question: value.question, header: typeof value.header === 'string' ? value.header : `问题 ${index + 1}`,
      options, multiSelect: value.multiSelect === true });
  }
  return result;
}

function splitSelections(value: string): string[] {
  return value
    .split(', ')
    .map((item) => item.trim())
    .filter(Boolean);
}

function isAnswerFromOptions(question: AskUserQuestion, answer: string): boolean {
  if (!answer) return false;
  if (answer === NO_PREFERENCE) return true;

  if (question.multiSelect) {
    const selected = splitSelections(answer);
    if (selected.length === 0) return false;
    const labels = new Set(question.options.map((opt) => opt.label));
    return selected.every((item) => labels.has(item));
  }

  return question.options.some((opt) => opt.label === answer);
}

export function QuestionCard({ questions: rawQuestions, onSubmit, disabled = false }: QuestionCardProps) {
  const parsedQuestions = useMemo(() => parseQuestions(rawQuestions), [rawQuestions]);
  const questions = parsedQuestions ?? EMPTY_QUESTIONS;
  // answers: question index -> selected label(s) or freeform text
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const [currentIndex, setCurrentIndex] = useState(0);

  const totalQuestions = questions.length;
  const safeIndex = Math.min(currentIndex, Math.max(totalQuestions - 1, 0));
  const currentQuestion = questions[safeIndex];
  const isLastQuestion = safeIndex === totalQuestions - 1;
  const currentAnswer = answers[String(safeIndex)] || '';

  const toggleOption = useCallback((qIdx: number, label: string, multiSelect?: boolean) => {
    setAnswers((prev) => {
      const key = String(qIdx);
      if (multiSelect) {
        const question = questions[qIdx];
        const validLabels = new Set((question?.options || []).map((opt) => opt.label));
        const current = prev[key] ? splitSelections(prev[key]).filter((item) => validLabels.has(item)) : [];
        const next = current.includes(label)
          ? current.filter((item) => item !== label)
          : [...current, label];
        return { ...prev, [key]: next.join(', ') };
      }
      return { ...prev, [key]: label };
    });
  }, [questions]);

  const setFreeform = useCallback((qIdx: number, text: string) => {
    setAnswers((prev) => ({ ...prev, [String(qIdx)]: text }));
  }, []);

  const submitAnswers = useCallback((sourceAnswers?: Record<string, string>) => {
    if (disabled || questions.length === 0) return;

    const answerSource = sourceAnswers || answers;
    // Build answers keyed by question text (matches backend ResumeRequest schema)
    const result: Record<string, string> = {};
    questions.forEach((q, i) => {
      result[q.question] = answerSource[String(i)]?.trim() || NO_PREFERENCE;
    });
    onSubmit(result);
  }, [answers, disabled, onSubmit, questions]);

  const handleSkip = useCallback(() => {
    if (disabled || questions.length === 0) return;

    const nextAnswers = {
      ...answers,
      [String(safeIndex)]: NO_PREFERENCE,
    };
    setAnswers(nextAnswers);

    if (isLastQuestion) {
      submitAnswers(nextAnswers);
      return;
    }

    setCurrentIndex((prev) => Math.min(prev + 1, totalQuestions - 1));
  }, [answers, disabled, isLastQuestion, questions.length, safeIndex, submitAnswers, totalQuestions]);

  const handlePrev = () => {
    if (disabled) return;
    setCurrentIndex((prev) => Math.max(prev - 1, 0));
  };

  const handleNext = () => {
    if (disabled) return;
    setCurrentIndex((prev) => Math.min(prev + 1, totalQuestions - 1));
  };

  if (!parsedQuestions) return <FeedbackMessage tone="error" messageKey={rawQuestions} className="rounded-xl border border-claude-border bg-white p-4 text-sm text-claude-error">
    问题内容不完整，请停止本轮后重试。
  </FeedbackMessage>;
  if (!currentQuestion) return null;

  const selectedValues = currentQuestion.multiSelect
    ? splitSelections(currentAnswer).filter((item) => currentQuestion.options.some((opt) => opt.label === item))
    : [];
  const freeformValue = isAnswerFromOptions(currentQuestion, currentAnswer) ? '' : currentAnswer;
  const canSubmit = !disabled && totalQuestions > 0;

  return (
    <div className="flex max-h-[min(560px,70dvh)] flex-col overflow-hidden rounded-2xl border border-claude-border bg-white shadow-sm">
      {/* Header */}
      <div className="flex shrink-0 items-center justify-between gap-2 border-b border-claude-border px-4 py-3">
        <div className="flex items-center gap-2 min-w-0">
          <MessageCircle size={16} className="text-claude-accent flex-shrink-0" />
          <span className="truncate text-sm font-semibold text-claude-text">请确认以下问题</span>
        </div>
        <div className="flex items-center gap-2 flex-shrink-0">
          {totalQuestions > 1 && (
            <span className="text-xs text-claude-secondary font-medium">
              {`${safeIndex + 1} / ${totalQuestions}`}
            </span>
          )}
        </div>
      </div>

      {/* Questions */}
      <div className="min-h-0 space-y-3 overflow-y-auto px-4 py-3">
        {/* Question header tag + text */}
        <div className="mb-2 flex flex-wrap items-center gap-2">
          <span className="inline-block px-1.5 py-0.5 rounded-md bg-claude-surface text-[11px] font-medium text-claude-secondary flex-shrink-0">
            {currentQuestion.header}
          </span>
          {currentQuestion.options.length > 0 && <span className="text-xs text-claude-secondary">
            {currentQuestion.multiSelect ? '可多选' : '单选'}
          </span>}
          <p className="w-full text-sm font-medium leading-relaxed text-claude-text">{currentQuestion.question}</p>
        </div>

        {/* Option buttons */}
        {currentQuestion.options.length === 0 && <p className="text-xs text-claude-secondary">本题未提供有效选项，请直接输入回答。</p>}
        <div className="space-y-1.5">
          {currentQuestion.options.map((opt) => {
            const selected = currentQuestion.multiSelect
              ? selectedValues.includes(opt.label)
              : currentAnswer === opt.label;

            return (
              <button
                key={opt.label}
                type="button"
                disabled={disabled}
                aria-pressed={selected}
                onClick={() => toggleOption(safeIndex, opt.label, currentQuestion.multiSelect)}
                className={`w-full text-left px-3 py-2 rounded-lg border transition-[background-color,border-color,box-shadow,opacity,transform] text-[13px] ${
                  selected
                    ? 'border-claude-text bg-claude-text/5 ring-1 ring-claude-text/20'
                    : 'border-claude-border hover:border-claude-border-strong hover:bg-claude-hover'
                } ${disabled ? 'opacity-50 cursor-not-allowed' : 'active:scale-[0.99]'}`}
              >
                <div className="flex items-start gap-2.5">
                  <div aria-hidden="true" className={`w-3.5 h-3.5 ${currentQuestion.multiSelect ? 'rounded-[3px]' : 'rounded-full'} border-2 flex items-center justify-center flex-shrink-0 mt-0.5 transition-colors ${
                    selected ? 'border-claude-text bg-claude-text' : 'border-claude-muted'
                  }`}>
                    {selected && (currentQuestion.multiSelect
                      ? <Check size={10} className="text-white" />
                      : <span className="h-1.5 w-1.5 rounded-full bg-white" />)}
                  </div>
                  <div className="flex-1 min-w-0">
                    <span className={`font-medium text-[13px] ${selected ? 'text-claude-text' : 'text-claude-secondary'}`}>
                      {opt.label}
                    </span>
                    {opt.description && (
                      <p className="text-[11px] text-claude-muted mt-0.5 leading-snug">{opt.description}</p>
                    )}
                  </div>
                </div>
              </button>
            );
          })}
        </div>

        {/* Freeform input */}
        <div className="mt-2">
          <input
            type="text"
            disabled={disabled}
            aria-label={currentQuestion.question}
            placeholder={currentQuestion.options.length ? '或输入自定义回答...' : '请输入回答...'}
            value={freeformValue}
            onChange={(e) => setFreeform(safeIndex, e.target.value)}
            className="w-full px-2.5 py-1.5 text-[13px] border border-claude-border rounded-lg bg-transparent text-claude-text placeholder:text-claude-muted focus:outline-none focus:border-claude-text/30 transition-colors disabled:opacity-50"
          />
        </div>
      </div>

      {/* Footer actions */}
      <div className="flex shrink-0 flex-wrap items-center justify-between gap-2 border-t border-claude-border px-4 py-3">
        <button
          type="button"
          disabled={disabled || safeIndex === 0}
          onClick={handlePrev}
          className={`inline-flex items-center justify-center gap-1 px-3 py-2 rounded-xl text-sm font-medium border transition-colors ${
            disabled || safeIndex === 0
              ? 'border-claude-border text-claude-muted bg-claude-surface cursor-not-allowed'
              : 'border-claude-border text-claude-secondary hover:border-claude-border-strong hover:bg-claude-hover'
          }`}
        >
          <ChevronLeft size={14} />
          上一题
        </button>

        <div className="flex items-center gap-2">
          <button
            type="button"
            disabled={disabled}
            onClick={handleSkip}
            className={`px-3 py-2 rounded-xl text-sm font-medium border transition-colors ${
              disabled
                ? 'border-claude-border text-claude-muted bg-claude-surface cursor-not-allowed'
                : 'border-claude-border text-claude-secondary hover:border-claude-border-strong hover:bg-claude-hover'
            }`}
          >
            {isLastQuestion ? '跳过并提交' : '跳过'}
          </button>

          {isLastQuestion ? (
            <button
              type="button"
              disabled={!canSubmit}
              onClick={() => submitAnswers()}
              className={`inline-flex items-center justify-center gap-2 px-4 py-2 rounded-xl text-sm font-medium transition-[color,background-color,transform] ${
                canSubmit
                  ? 'bg-claude-text text-white hover:bg-claude-text/90 active:scale-[0.98]'
                  : 'bg-claude-surface text-claude-muted cursor-not-allowed'
              }`}
            >
              <Send size={14} />
              提交
            </button>
          ) : (
            <button
              type="button"
              disabled={disabled}
              onClick={handleNext}
              className={`inline-flex items-center justify-center gap-1 px-3 py-2 rounded-xl text-sm font-medium transition-[color,background-color,transform] ${
                disabled
                  ? 'bg-claude-surface text-claude-muted cursor-not-allowed'
                  : 'bg-claude-text text-white hover:bg-claude-text/90 active:scale-[0.98]'
              }`}
            >
              下一题
              <ChevronRight size={14} />
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

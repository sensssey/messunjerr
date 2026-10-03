import React, { useState } from "react";
import { Post } from "../../types/posts";

interface EditPostFormProps {
  post: Post; 
  onSubmit: (updatedData: { title: string; content: string }) => void; 
}

const EditPostForm = ({ post, onSubmit }: EditPostFormProps) => {
  const [title, setTitle] = useState(post.title); // Начальное значение заголовка
  const [content, setContent] = useState(post.content); // Начальное значение содержимого

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    onSubmit({ title, content }); 
  };

  return (
    <form onSubmit={handleSubmit} className="space-y-4">
      <div>
        <label className="block mb-1 text-sm font-medium text-gray-700">Title</label>
        <input
          type="text"
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          className="w-full border border-gray-300 rounded p-2 text-sm placeholder-gray-500 focus:outline-none focus:ring-2 focus:ring-blue-500"
          placeholder="Enter post title"
          required
        />
      </div>

      <div>
        <label className="block mb-1 text-sm font-medium text-gray-700">Content</label>
        <textarea
          value={content}
          onChange={(e) => setContent(e.target.value)}
          className="w-full border border-gray-300 rounded p-2 text-sm placeholder-gray-500 focus:outline-none focus:ring-2 focus:ring-blue-500"
          placeholder="Enter post content"
          rows={4}
          required
        />
      </div>

      <button
        type="submit"
        className="bg-green-500 text-white py-2 rounded w-full hover:bg-green-600 transition duration-300"
      >
        Update Post
      </button>
    </form>
  );
};

export default EditPostForm;
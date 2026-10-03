import React from "react";
import Modal from "./Modal";
import { Post } from "../../types/posts";
import { updatePost } from "../../services/api";

interface EditPostModalProps {
  isOpen: boolean;
  onClose: () => void;
  post: Post | null;
  setPosts: React.Dispatch<React.SetStateAction<Post[]>>;
}

const EditPostModal = ({ isOpen, onClose, post, setPosts }: EditPostModalProps) => {
  if (!post) return null;

  const handleSubmit = async (updatedData: { title: string; content: string }) => {
    try {
      const updatedPost = await updatePost(Number(post.id), updatedData);
      console.log("Post updated successfully!");
      setPosts((prevPosts) =>
        prevPosts.map((p) => (Number(p.id) === Number(updatedPost.id) ? updatedPost : p))
      );
      onClose();
    } catch (error) {
      console.error("Error updating post:", error);
    }
  };

  return (
    <Modal isOpen={isOpen} onClose={onClose}>
      <h2 className="text-xl font-bold text-gray-800 mb-4">Edit Post</h2>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          const form = e.target as HTMLFormElement;
          const formData = new FormData(form);
          const title = formData.get("title") as string;
          const content = formData.get("content") as string;
          handleSubmit({ title, content });
        }}
        className="space-y-4"
      >
        <div>
          <label className="block mb-1 text-sm font-medium text-gray-700">Title</label>
          <input
            type="text"
            name="title"
            defaultValue={post.title}
            className="w-full border border-gray-300 rounded p-2 text-sm placeholder-gray-500 focus:outline-none focus:ring-2 focus:ring-blue-500"
            placeholder="Enter post title"
            required
          />
        </div>

        <div>
          <label className="block mb-1 text-sm font-medium text-gray-700">Content</label>
          <textarea
            name="content"
            defaultValue={post.content}
            className="w-full border border-gray-300 rounded p-2 text-sm placeholder-gray-500 focus:outline-none focus:ring-2 focus:ring-blue-500"
            placeholder="Enter post content"
            rows={4}
            required
          />
        </div>

        <button
          type="submit"
          className="bg-gray-800 text-white py-2 rounded w-full hover:bg-gray-600 transition duration-300"
        >
          Update Post
        </button>
      </form>
    </Modal>
  );
};

export default EditPostModal;